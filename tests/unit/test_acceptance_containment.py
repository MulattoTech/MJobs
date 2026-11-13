"""Injected Docker/API safety contracts for owned public-source containment."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from cli.acceptance_containment import (
    API_TIMEOUT_SECONDS,
    CONTAINER_INSPECT_TEMPLATE,
    FINGERPRINT_FIELDS,
    HEALTH_RESTORE_TIMEOUT_SECONDS,
    READINESS_DEPENDENCIES,
    READINESS_WORKERS,
    ContainmentError,
    DockerCLI,
    OwnedPublicContainment,
)
from cli.acceptance_public_source import run as run_public_source, child_containment_context, _digest
from services.discovery.sources.registry import source_adapter

STAMP = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
PROJECT = "mjobs-fixture-012345abcdef"
IMAGE_ID = "sha256:" + "a" * 64
REVISION = "1" * 40
TREE = "2" * 40
OWNER = "mjreq292-test-operator"
SOURCE_URL = "https://github.com/MulattoTech/MJobs"
ADDITIONAL_POLICY = {
    "provider": "remotive",
    "tenant": "default",
    "enabled": True,
    "poll_interval": 604800,
    "concurrency": 1,
    "request_timeout": 10,
    "rate_limit": 1,
    "jitter_seconds": 0,
}


def additional_source_record(source_id, *, enabled=True, provider="remotive"):
    capabilities = source_adapter({**ADDITIONAL_POLICY, "provider": provider}).capabilities()
    return {
        "id": source_id,
        "provider": provider,
        "tenant": "default",
        "enabled": enabled,
        "poll_interval": 604800,
        "concurrency": 1,
        "request_timeout": 10,
        "rate_limit": 1,
        "jitter_seconds": 0,
        "strategy": capabilities.strategy.value,
        "capabilities": capabilities.as_dict(),
        "last_attempt": None,
        "last_success": None,
        "backoff_until": None,
        "health_status": "NOT_CHECKED",
        "jobs_seen": 0,
    }


class FakeDocker:
    def __init__(self):
        self.containers = {"api": "a" * 64, "scheduler": "b" * 64}
        self.rows = {
            service: self._container(service, container_id)
            for service, container_id in self.containers.items()
        }
        self.pause_calls = []
        self.unpause_calls = []
        self.on_pause = lambda: None
        self.on_unpause = lambda: None

    def _container(self, service, container_id):
        labels = {
            "com.docker.compose.project": PROJECT,
            "com.docker.compose.service": service,
            "com.docker.compose.project.config_files": "C:/campaign/compose.yml,C:/campaign/compose.override.yml",
            "com.docker.compose.project.working_dir": "C:/campaign",
            "org.opencontainers.image.source": SOURCE_URL,
            "org.opencontainers.image.revision": REVISION,
            "org.opencontainers.image.source-tree": TREE,
        }
        ports = (
            {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18732"}]}
            if service == "api"
            else {}
        )
        return {
            "id": container_id,
            "image_id": IMAGE_ID,
            "image_ref": "mjobs-292:test",
            "working_dir": "/app",
            "labels": labels,
            "status": "running",
            "paused": False,
            "health": "healthy",
            "ports": ports,
        }

    def list_service_containers(self, project, service):
        if project != PROJECT:
            return []
        container_id = self.containers.get(service)
        return [container_id] if container_id else []

    def inspect(self, container_id):
        for row in self.rows.values():
            if row["id"] == container_id:
                return deepcopy(row)
        raise AssertionError("unexpected inspect target")

    def pause(self, container_id):
        self.pause_calls.append(container_id)
        self.rows["scheduler"]["paused"] = True
        self.rows["scheduler"]["status"] = "paused"
        self.rows["scheduler"]["health"] = "unhealthy"
        self.on_pause()

    def unpause(self, container_id):
        self.unpause_calls.append(container_id)
        self.rows["scheduler"]["paused"] = False
        self.rows["scheduler"]["status"] = "running"
        self.rows["scheduler"]["health"] = "healthy"
        self.rows["api"]["health"] = "healthy"
        self.on_unpause()


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.sources = []
        self.operations = []
        self.health = {
            "status": "ok", "check": "liveness", "mode": "LOCAL_FIRST",
            "operation_mode": "FIXTURE", "external_submission_enabled": False,
        }
        self.ready_status = 200
        self.ready = {
            "status": "ready", "db_backend": "postgresql",
            "checks": {
                **dict.fromkeys(READINESS_DEPENDENCIES, True),
                "scheduler": True, "workers": dict.fromkeys(READINESS_WORKERS, True),
            },
        }
        self.profile = {"id": "profile", "version": "v1"}
        self.app = {
            "id": "application",
            "profile_id": "profile",
            "job_id": "fixture-job",
            "route_id": "fixture-route",
            "target": "MOCK",
            "confirmation": {"receipt": "mock"},
        }
        self.job = {
            "id": "fixture-job",
            "attributes": {"fictional": True},
            "routes": [{"id": "fixture-route", "provider": "mock-ats"}],
        }
        raw = {"id": "provider-job", "title": "Public role"}
        checksum = hashlib.sha256(
            json.dumps(
                raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.public = {
            "id": "real-job",
            "attributes": {
                "fictional": False,
                "provider": "wwr",
                "source_id": "public-source",
                "source_endpoint": "https://weworkremotely.com/remote-jobs.rss",
                "native_posting_id": "provider-job",
                "submission_capability": "MANUAL_ONLY",
            },
            "match": {"profile_id": "profile", "profile_version": "v1"},
            "postings": [
                {
                    "id": "public-posting",
                    "source": "https://weworkremotely.com/remote-jobs.rss",
                    "fetched_at": "2026-11-06T12:00:00+00:00",
                    "raw": raw,
                    "snapshots": [
                        {
                            "id": "snapshot",
                            "sha256": checksum,
                            "retrieved_at": "2026-11-06T12:00:00+00:00",
                        }
                    ],
                }
            ],
        }
        self.collection_state = "QUEUED"

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, deepcopy(kwargs)))
        if (method, path) == ("GET", "/settings"):
            return {"operation_mode": "FIXTURE", "external_submission_enabled": False}
        if (method, path) == ("GET", "/health"):
            return deepcopy(self.health)
        if (method, path) == ("GET", "/ready"):
            return httpx.Response(
                self.ready_status, json=deepcopy(self.ready),
                request=httpx.Request("GET", "http://127.0.0.1:18732/ready"),
            )
        if (method, path) == ("GET", "/company-sources"):
            return {"company_sources": []}
        if (method, path) == ("GET", "/operations"):
            return {"operations": deepcopy(self.operations)}
        if (method, path) == ("GET", "/sources"):
            return {"sources": deepcopy(self.sources)}
        if (method, path) == ("PUT", "/sources"):
            if not self.sources:
                capabilities = source_adapter(kwargs["json"]).capabilities()
                self.sources = [
                    {
                        "id": "public-source",
                        "strategy": capabilities.strategy.value,
                        "capabilities": capabilities.as_dict(),
                        "last_attempt": None,
                        "last_success": None,
                        "backoff_until": None,
                        "health_status": "NOT_CHECKED",
                        "jobs_seen": 0,
                        **deepcopy(kwargs["json"]),
                    }
                ]
            else:
                self.sources[0].update(deepcopy(kwargs["json"]))
            return {"id": "public-source"}
        if path == "/profiles/profile" and method == "GET":
            return deepcopy(self.profile)
        if path == "/profiles/profile/applications/application" and method == "GET":
            return deepcopy(self.app)
        if path == "/jobs/fixture-job" and method == "GET":
            return deepcopy(self.job)
        if path == "/jobs/real-job" and method == "GET":
            return deepcopy(self.public)
        if path == "/jobs" and method == "GET":
            return {"jobs": [{"id": "real-job"}]}
        if method == "POST" and path == "/sources/public-source/collect":
            self.sources[0].update(
                last_attempt="2026-11-06T12:00:00+00:00",
                last_success="2026-11-06T12:00:00+00:00",
                health_status="HEALTHY",
                jobs_seen=1,
            )
            params = {
                "source_id": "public-source",
                "request_id": kwargs["json"]["request_id"],
                "profile_version": "v1",
            }
            self.operations.append(
                {
                    "id": "own-operation",
                    "kind": "DISCOVER",
                    "entity_id": "profile",
                    "state": self.collection_state,
                    "attempt_count": 1,
                    "params": params,
                }
            )
            return {"operation_id": "own-operation"}
        if path == "/operations/own-operation" and method == "GET":
            return {
                "id": "own-operation",
                "kind": "DISCOVER",
                "entity_id": "profile",
                "state": self.collection_state,
                "params": deepcopy(self.operations[0]["params"]),
                "attempt_count": 1,
                "retry_count": 0,
                "started_at": "2026-11-06T12:00:00+00:00",
            }
        if path == "/profiles/profile/sources/public-source/collections/own-operation/children" and method == "GET":
            assert kwargs.get("params") == {"attempt": 1}
            parent = next(row for row in self.operations if row["id"] == "own-operation")
            assert parent["params"]["source_id"] == "public-source"
            result = dict(schema_version=1, profile_id=parent["entity_id"],
                profile_version=parent["params"]["profile_version"], source_id="public-source",
                parent_operation_id=parent["id"], parent_operation_attempt=1,
                parent_request_id=parent["params"]["request_id"],
                complete=parent["state"] == "COMPLETED", limit=2,
                children=[], enrolled_sources=[], lineage_sha256=_digest([]))
            result["sha256"] = _digest(result)
            return result
        raise AssertionError((method, path, kwargs))


class OwnedPublicContainmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        (self.root / "outputs").mkdir()
        self.run_dir = self.root / "outputs" / "containment-run"
        self.run_dir.mkdir()
        (self.root / ".gitignore").write_text("outputs/\n", encoding="utf-8")
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.docker = FakeDocker()
        self.compose = (self.root / "compose.yml", self.root / "compose.override.yml")
        for path in self.compose:
            path.touch()
        for row in self.docker.rows.values():
            row["labels"]["com.docker.compose.project.config_files"] = ",".join(
                str(path.resolve()) for path in self.compose
            )
            row["labels"]["com.docker.compose.project.working_dir"] = str(
                self.root.resolve()
            )
        self.api = FakeAPI()
        self.now = [STAMP]
        self.clock = [0.0]

    def containment(self, **kwargs):
        containment_class = kwargs.pop("containment_class", OwnedPublicContainment)
        baseline = kwargs.pop(
            "baseline",
            {
                "profile_id": "profile",
                "application_id": "application",
                "selected_application_job_id": "fixture-job",
                "selected_application_route_id": "fixture-route",
                "confirmation": self.api.app["confirmation"],
                "external_submission_enabled": False,
            },
        )
        return containment_class(
            project_name=PROJECT,
            base_url="http://127.0.0.1:18732",
            source_label=SOURCE_URL,
            image_ref="mjobs-292:test",
            image_id=IMAGE_ID,
            source_revision=REVISION,
            source_tree=TREE,
            compose_working_dir=self.root,
            container_working_dir="/app",
            compose_files=self.compose,
            run_dir=self.run_dir,
            repository_root=self.root,
            baseline=baseline,
            request=self.api.request,
            docker=self.docker,
            owner=OWNER,
            lease_seconds=16,
            utc_now=lambda: self.now[0],
            sleep=self._sleep,
            monotonic=lambda: self.clock[0],
            **kwargs,
        )

    def _sleep(self, seconds):
        self.clock[0] += seconds
        self.now[0] += timedelta(seconds=seconds)

    @staticmethod
    def context(**overrides):
        return {"provider": "wwr", "base_url": "http://127.0.0.1:18732", **overrides}

    def test_docker_cli_projection_parses_only_allowlisted_fields(self):
        labels = [
            PROJECT,
            "api",
            ",".join(str(path.resolve()) for path in self.compose),
            str(self.root.resolve()),
            SOURCE_URL,
            REVISION,
            TREE,
        ]
        projection = "|".join(
            [
                "a" * 64,
                IMAGE_ID,
                "mjobs-292:test",
                "/app",
                *labels,
                "running",
                "false",
                "healthy",
                '{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"18732"}]}',
            ]
        )
        with patch(
            "cli.acceptance_containment._run_docker", return_value=projection
        ) as run:
            observed = DockerCLI().inspect("a" * 64)
        self.assertEqual(observed["image_id"], IMAGE_ID)
        self.assertEqual(
            observed["labels"]["org.opencontainers.image.source-tree"], TREE
        )
        self.assertEqual(observed["ports"]["8000/tcp"][0]["HostPort"], "18732")
        self.assertNotIn(".Config.Env", CONTAINER_INSPECT_TEMPLATE)
        self.assertNotIn("Config.Env", " ".join(run.call_args.args[0]))

    def test_preflight_claims_and_pauses_only_the_exact_owned_scheduler(self):
        containment = self.containment()
        proof = containment("preflight", self.context())
        self.assertTrue(proof["scheduler_contained"])
        self.assertTrue(proof["source_policy_exclusive"])
        self.assertTrue(proof["source_collection_exclusive"])
        self.assertEqual(proof["owner"], OWNER)
        self.assertEqual(
            proof["lease_expires_at"], (STAMP + timedelta(seconds=16)).isoformat()
        )
        self.assertEqual(self.docker.pause_calls, ["b" * 64])
        self.assertEqual(self.docker.unpause_calls, [])
        claim = json.loads(containment.claim_path.read_text(encoding="utf-8"))
        self.assertEqual(claim["project"], PROJECT)
        self.assertEqual(claim["scheduler_container_id"], "b" * 64)
        self.assertEqual(claim["owner"], OWNER)
        self.assertEqual(claim["token"], proof["token"])

    def test_preexisting_active_discovery_blocks_before_claim_or_scheduler_mutation(
        self,
    ):
        self.api.operations = [
            {
                "id": "other",
                "kind": "DISCOVER",
                "state": "RUNNING",
                "entity_id": "other-profile",
                "params": {"source_id": "other-source"},
            }
        ]
        containment = self.containment()
        with self.assertRaisesRegex(ContainmentError, "ACTIVE_COLLECTION_EXISTS"):
            containment("preflight", self.context())
        self.assertEqual(self.docker.pause_calls, [])
        self.assertFalse(containment.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, [])

    def test_collection_race_after_pause_retains_claim_and_scheduler_pause(self):
        self.docker.on_pause = lambda: self.api.operations.append(
            {
                "id": "raced",
                "kind": "DISCOVER",
                "state": "QUEUED",
                "entity_id": "other-profile",
                "params": {"source_id": "other-source"},
            }
        )
        containment = self.containment()
        with self.assertRaisesRegex(ContainmentError, "ACTIVE_COLLECTION_EXISTS"):
            containment("preflight", self.context())
        self.assertEqual(self.docker.pause_calls, ["b" * 64])
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, [])

    def test_existing_claim_is_never_replaced_or_scheduler_paused(self):
        claim = self.run_dir / "public-source-containment.json"
        claim.write_text('{"owner":"other"}\n', encoding="utf-8")
        containment = self.containment()
        with self.assertRaisesRegex(ContainmentError, "CLAIM_ALREADY_EXISTS"):
            containment("preflight", self.context())
        self.assertEqual(claim.read_text(encoding="utf-8"), '{"owner":"other"}\n')
        self.assertEqual(self.docker.pause_calls, [])

    def test_unknown_callback_stage_fails_before_claim_or_container_mutation(self):
        containment = self.containment()
        with self.assertRaisesRegex(ContainmentError, "CONTAINMENT_STAGE_INVALID"):
            containment("start-foreign-container", self.context())
        self.assertEqual(self.docker.pause_calls, [])
        self.assertFalse(containment.claim_path.exists())

    def test_nonrunning_or_already_paused_scheduler_is_never_started_or_unpaused(self):
        for status, paused in (("exited", False), ("paused", True)):
            with self.subTest(status=status):
                self.docker = FakeDocker()
                self.docker.rows["scheduler"]["status"] = status
                self.docker.rows["scheduler"]["paused"] = paused
                containment = self.containment()
                with self.assertRaises(ContainmentError):
                    containment("preflight", self.context())
                self.assertEqual(self.docker.pause_calls, [])
                self.assertEqual(self.docker.unpause_calls, [])

    def test_runtime_identity_mismatch_fails_before_scheduler_mutation(self):
        cases = (
            (
                "label",
                "org.opencontainers.image.source",
                "https://example.invalid/repo",
            ),
            ("label", "org.opencontainers.image.revision", "f" * 40),
            ("label", "org.opencontainers.image.source-tree", "f" * 40),
            ("label", "com.docker.compose.project.working_dir", "C:/foreign"),
            ("label", "com.docker.compose.project", "mjobs-fixture-ffffffffffff"),
            ("label", "com.docker.compose.service", "worker"),
            (
                "label",
                "com.docker.compose.project.config_files",
                "C:/foreign/compose.yml",
            ),
            ("image_id", "image_id", "sha256:" + "f" * 64),
            ("image_ref", "image_ref", "mjobs-292:other"),
        )
        for target, label, value in cases:
            with self.subTest(target=target, label=label):
                self.docker = FakeDocker()
                if target == "label":
                    self.docker.rows["scheduler"]["labels"][label] = value
                else:
                    self.docker.rows["scheduler"][target] = value
                containment = self.containment()
                with self.assertRaises(ContainmentError):
                    containment("preflight", self.context())
                self.assertEqual(self.docker.pause_calls, [])

    def test_api_must_be_bound_to_the_configured_loopback_port(self):
        cases = (
            {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18732"}]},
            {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18732"}],
                "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}],
            },
        )
        for bindings in cases:
            with self.subTest(bindings=bindings):
                self.docker = FakeDocker()
                self.docker.rows["api"]["ports"] = bindings
                containment = self.containment()
                with self.assertRaises(ContainmentError):
                    containment("preflight", self.context())
                self.assertEqual(self.docker.pause_calls, [])

    def test_only_the_matching_queued_collection_is_allowed(self):
        self.api.sources = [
            {
                "id": "public-source",
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
                "strategy": "FEED",
                "capabilities": {"direct_application": False},
            }
        ]
        containment = self.containment()
        proof = containment("preflight", self.context())
        self.assertEqual(
            containment.request(
                "POST",
                "/sources/public-source/collect",
                json={
                    "profile_id": "profile",
                    "request_id": "public-observation-owned",
                },
            ),
            {"operation_id": "own-operation"},
        )
        self.api.operations.append(
            {
                "id": "unrelated",
                "kind": "DISCOVER",
                "state": "QUEUED",
                "entity_id": "another-profile",
                "params": {
                    "source_id": "public-source",
                    "request_id": "another-request",
                },
            }
        )
        with self.assertRaisesRegex(ContainmentError, "ACTIVE_COLLECTION_EXISTS"):
            containment("poll", self.context(source_id="public-source"))
        self.assertTrue(proof["scheduler_contained"])
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_collection_identity_from_post_context_is_sufficient_without_interception(
        self,
    ):
        self.api.sources = [
            {
                "id": "public-source",
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
                "strategy": "FEED",
                "capabilities": {"direct_application": False},
            }
        ]
        containment = self.containment()
        containment("preflight", self.context())
        self.api.operations = [
            {
                "id": "response-operation",
                "kind": "DISCOVER",
                "entity_id": "profile",
                "state": "QUEUED",
                "params": {
                    "source_id": "public-source",
                    "request_id": "public-observation-request",
                },
            }
        ]

        joined_context = self.context(
            source_id="public-source",
            source_policy_sha256=containment._policy_digest(self.api.sources[0]),
            newly_created=False,
            collection_operation_id="response-operation",
            collection_request_id="public-observation-request",
        )
        proof = containment("poll", joined_context)

        self.assertTrue(proof["source_collection_exclusive"])
        self.assertEqual(containment._own_operation_id, "response-operation")
        self.assertEqual(containment._own_request_id, "public-observation-request")
        self.api.operations[0]["params"]["request_id"] = "another-request"
        with self.assertRaisesRegex(ContainmentError, "ACTIVE_COLLECTION_EXISTS"):
            containment("poll", joined_context)
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())
        self.api.operations[0]["params"]["request_id"] = "public-observation-request"
        self.api.operations[0]["state"] = "COMPLETED"

        released = containment(
            "release", {**joined_context, "cleanup_status": "NOT_REQUIRED"}
        )

        self.assertTrue(released["released"])
        self.assertFalse(released["source_collection_exclusive"])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])

    def test_new_source_identity_from_post_context_is_validated_against_inventory(self):
        containment = self.containment()
        containment("preflight", self.context())
        self.api.sources = [
            {
                "id": "created-source",
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
                "strategy": "FEED",
                "capabilities": {"direct_application": False},
            }
        ]

        proof = containment(
            "read",
            self.context(source_id="created-source", newly_created=True),
        )

        self.assertTrue(proof["source_policy_exclusive"])
        self.assertEqual(containment._source["id"], "created-source")

    def test_new_source_cleanup_is_confirmed_by_exact_api_disable_transition(self):
        containment = self.containment()
        containment("preflight", self.context())
        self.api.sources = [
            {
                "id": "created-source",
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
                "strategy": "FEED",
                "capabilities": {"direct_application": False},
            }
        ]
        enabled_policy_hash = containment._policy_digest(self.api.sources[0])
        context = self.context(
            source_id="created-source",
            source_policy_sha256=enabled_policy_hash,
            newly_created=True,
        )
        containment("read", context)
        containment("cleanup", context)
        containment("cleanup", context)
        self.api.sources[0]["enabled"] = False

        proof = containment("read", context)

        self.assertTrue(containment._disable_attempted)
        self.assertTrue(proof["scheduler_contained"])
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, [])

        released = containment(
            "release", {**context, "cleanup_status": "RESTORED_DISABLED"}
        )

        self.assertTrue(released["released"])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])

    def _exercise_additional_source_lifecycle(self, primary_provider, extra_provider):
        containment = self.containment()
        primary = self.context(provider=primary_provider)
        containment("preflight", primary)

        proof = containment.bind_additional_source(
            policy={**ADDITIONAL_POLICY, "provider": extra_provider}, primary_context=primary
        )

        self.assertTrue(proof["additional_source_absent"])
        self.assertEqual(proof["provider"], extra_provider)
        absent_context = {
            **primary,
            "additional_source": {
                "provider": extra_provider,
                "tenant": "default",
                "source_id": None,
                "enabled_policy_sha256": None,
                "disabled_policy_sha256": None,
                "enabled": False,
            },
        }
        containment("read", absent_context)
        containment.additional_source("check", primary)
        enabled_source = additional_source_record(extra_provider + "-source", provider=extra_provider)
        self.api.sources = [enabled_source]
        enabled_hash = containment.policy_sha256(enabled_source)
        enabled_context = {
            **primary,
            "additional_source_id": extra_provider + "-source",
            "additional_enabled_policy_sha256": enabled_hash,
        }
        created = containment.additional_source("created", enabled_context)
        self.assertTrue(created["additional_source_policy_verified"])
        self.assertEqual(created["additional_source_id"], extra_provider + "-source")

        containment.additional_source("before_disable", enabled_context)
        self.api.sources[0]["enabled"] = False
        disabled_hash = containment.policy_sha256(self.api.sources[0])
        disabled_context = {
            **enabled_context,
            "additional_disabled_policy_sha256": disabled_hash,
            "additional_enabled": False,
        }
        disabled = containment.additional_source("disabled", disabled_context)
        self.assertFalse(disabled["additional_source_enabled"])

        nested_context = {
            "provider": extra_provider,
            "tenant": "default",
            "source_id": extra_provider + "-source",
            "enabled_policy_sha256": enabled_hash,
            "disabled_policy_sha256": disabled_hash,
            "enabled": False,
        }
        callback_context = {**primary, "additional_source": nested_context}
        containment("read", callback_context)
        containment.additional_source("check", disabled_context)
        containment.additional_source("release", disabled_context)
        released = containment(
            "release",
            {
                **callback_context,
                "cleanup_status": "NOT_REQUIRED",
            },
        )

        self.assertTrue(released["released"])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])

    def test_additional_remotive_source_is_bound_created_disabled_and_released(self):
        self._exercise_additional_source_lifecycle("wwr", "remotive")

    def test_additional_wwr_source_is_bound_created_disabled_and_released(self):
        self._exercise_additional_source_lifecycle("remotive", "wwr")

    def test_additional_source_race_is_not_adopted_or_disabled(self):
        self._exercise_additional_source_race_is_not_adopted_or_disabled("wwr", "remotive")

    def test_remotive_primary_additional_source_race_is_not_adopted_or_disabled(self):
        self._exercise_additional_source_race_is_not_adopted_or_disabled("remotive", "wwr")

    def _exercise_additional_source_race_is_not_adopted_or_disabled(self, primary_provider, extra_provider):
        containment = self.containment()
        primary = self.context(provider=primary_provider)
        containment("preflight", primary)
        containment.bind_additional_source(
            policy={**ADDITIONAL_POLICY, "provider": extra_provider}, primary_context=primary
        )
        self.api.sources = [additional_source_record("competing-source", provider=extra_provider)]
        competing_hash = containment._policy_digest(self.api.sources[0])

        with self.assertRaisesRegex(
            ContainmentError, "ADDITIONAL_SOURCE_IDENTITY_CHANGED"
        ):
            containment.additional_source(
                "created",
                {
                    **primary,
                    "additional_source_id": "ui-returned-source",
                    "additional_enabled_policy_sha256": competing_hash,
                },
            )

        self.assertEqual(self.api.sources[0]["id"], "competing-source")
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_additional_source_policy_mutation_keeps_containment(self):
        self._exercise_additional_source_policy_mutation_keeps_containment("wwr", "remotive")

    def test_remotive_primary_additional_source_policy_mutation_keeps_containment(self):
        self._exercise_additional_source_policy_mutation_keeps_containment("remotive", "wwr")

    def _exercise_additional_source_policy_mutation_keeps_containment(self, primary_provider, extra_provider):
        containment = self.containment()
        primary = self.context(provider=primary_provider)
        containment("preflight", primary)
        containment.bind_additional_source(
            policy={**ADDITIONAL_POLICY, "provider": extra_provider}, primary_context=primary
        )
        source = additional_source_record("extra-source", provider=extra_provider)
        self.api.sources = [source]
        context = {
            **primary,
            "additional_source_id": source["id"],
            "additional_enabled_policy_sha256": containment._policy_digest(source),
        }
        containment.additional_source("created", context)
        containment.additional_source("before_disable", context)
        self.api.sources[0]["rate_limit"] = 7

        with self.assertRaisesRegex(
            ContainmentError, "ADDITIONAL_SOURCE_POLICY_CHANGED"
        ):
            containment.additional_source("before_disable", context)

        self.assertEqual(self.api.sources[0]["rate_limit"], 7)
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_main_callbacks_require_bound_additional_source_context(self):
        self._exercise_main_callbacks_require_bound_additional_source_context("wwr", "remotive")

    def test_remotive_primary_main_callbacks_require_bound_additional_source_context(self):
        self._exercise_main_callbacks_require_bound_additional_source_context("remotive", "wwr")

    def _exercise_main_callbacks_require_bound_additional_source_context(self, primary_provider, extra_provider):
        containment = self.containment()
        primary = self.context(provider=primary_provider)
        containment("preflight", primary)
        containment.bind_additional_source(
            policy={**ADDITIONAL_POLICY, "provider": extra_provider}, primary_context=primary
        )

        with self.assertRaisesRegex(
            ContainmentError, "ADDITIONAL_SOURCE_CONTEXT_REQUIRED"
        ):
            containment("read", primary)

        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_source_appearing_before_latched_create_fails_closed(self):
        self._exercise_source_appearing_before_latched_create_fails_closed("wwr", "remotive")

    def test_remotive_primary_source_appearing_before_latched_create_fails_closed(self):
        self._exercise_source_appearing_before_latched_create_fails_closed("remotive", "wwr")

    def _exercise_source_appearing_before_latched_create_fails_closed(self, primary_provider, extra_provider):
        containment = self.containment()
        primary = self.context(provider=primary_provider)
        containment("preflight", primary)
        containment.bind_additional_source(
            policy={**ADDITIONAL_POLICY, "provider": extra_provider}, primary_context=primary
        )
        self.api.sources = [additional_source_record("unexpected-source", provider=extra_provider)]
        absent_context = {
            **primary,
            "additional_source": {
                "provider": extra_provider,
                "tenant": "default",
                "source_id": None,
                "enabled_policy_sha256": None,
                "disabled_policy_sha256": None,
                "enabled": False,
            },
        }

        with self.assertRaisesRegex(
            ContainmentError, "ADDITIONAL_SOURCE_APPEARED_BEFORE_CREATE"
        ):
            containment("read", absent_context)

        self.assertEqual(self.api.sources[0]["id"], "unexpected-source")
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def _exercise_additional_source_rejects_unsafe_pair_and_changed_primary(self, primary_provider, extra_provider):
        containment = self.containment()
        primary = self.context(provider=primary_provider)
        containment("preflight", primary)
        expected_policy = {**ADDITIONAL_POLICY, "provider": extra_provider}
        with self.assertRaisesRegex(ContainmentError, "SOURCE_SCOPE_CHANGED"):
            containment.bind_additional_source(
                policy={**ADDITIONAL_POLICY, "provider": primary_provider},
                primary_context={**primary, "provider": extra_provider})
        self.assertEqual(containment._provider, primary_provider)
        for change in ({"provider": primary_provider}, {"provider": "remoteok"},
                       {"tenant": "operator"}, {"rate_limit": 5}):
            with self.subTest(change=change), self.assertRaisesRegex(
                    ContainmentError, "ADDITIONAL_SOURCE_POLICY_UNSAFE"):
                containment.bind_additional_source(policy={**expected_policy, **change},
                                                  primary_context=primary)
            self.assertIsNone(containment._additional_policy)
        containment.bind_additional_source(policy=expected_policy, primary_context=primary)
        with self.assertRaisesRegex(ContainmentError, "SOURCE_SCOPE_CHANGED"):
            containment.additional_source("check", {**primary, "provider": extra_provider})
        nested = {"provider": extra_provider, "tenant": "default", "source_id": None,
                  "enabled_policy_sha256": None, "disabled_policy_sha256": None, "enabled": False}
        for context in ({**primary, "provider": extra_provider, "additional_source": nested},
                        {**primary, "additional_source": {**nested, "provider": primary_provider}}):
            with self.assertRaises(ContainmentError):
                containment("read", context)
        self.assertEqual(self.api.sources, [])
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_additional_source_rejects_unsafe_pair_and_changed_primary(self):
        self._exercise_additional_source_rejects_unsafe_pair_and_changed_primary("wwr", "remotive")

    def test_remotive_primary_rejects_unsafe_pair_and_changed_primary(self):
        self._exercise_additional_source_rejects_unsafe_pair_and_changed_primary("remotive", "wwr")

    def _exercise_additional_source_rejects_forged_capabilities(self, primary_provider, extra_provider):
        containment = self.containment()
        primary = self.context(provider=primary_provider)
        containment("preflight", primary)
        containment.bind_additional_source(policy={**ADDITIONAL_POLICY, "provider": extra_provider},
                                           primary_context=primary)
        source = additional_source_record("extra-source", provider=extra_provider)
        source["capabilities"] = source_adapter({**ADDITIONAL_POLICY, "provider": primary_provider}).capabilities().as_dict()
        source["strategy"] = source["capabilities"]["strategy"]
        self.api.sources = [source]
        with self.assertRaisesRegex(ContainmentError, "ADDITIONAL_SOURCE_POLICY_UNSAFE"):
            containment.additional_source("created", {**primary, "additional_source_id": source["id"],
                "additional_enabled_policy_sha256": containment.policy_sha256(source)})
        self.assertEqual(containment._additional_state, "ABSENT")
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_additional_source_rejects_forged_capabilities(self):
        self._exercise_additional_source_rejects_forged_capabilities("wwr", "remotive")

    def test_remotive_primary_rejects_forged_capabilities(self):
        self._exercise_additional_source_rejects_forged_capabilities("remotive", "wwr")

    def test_callback_returns_stable_owner_token_for_each_supported_stage(self):
        containment = self.containment()
        initial = containment("preflight", self.context())
        for stage in (
            "enable",
            "dispatch",
            "poll",
            "read",
            "finalize",
            "cleanup",
        ):
            with self.subTest(stage=stage):
                proof = containment(stage, self.context())
                self.assertEqual(
                    (proof["owner"], proof["token"]),
                    (initial["owner"], initial["token"]),
                )
                self.assertGreater(
                    datetime.fromisoformat(proof["lease_expires_at"]),
                    self.now[0] + timedelta(seconds=15),
                )

    def test_new_source_policy_can_only_be_enabled_once_then_disabled_for_cleanup(self):
        containment = self.containment()
        context = self.context()
        containment("preflight", context)
        containment("enable", context)
        policy = {
            "provider": "wwr",
            "tenant": "default",
            "enabled": True,
            "poll_interval": 604800,
            "concurrency": 1,
            "request_timeout": 10,
            "rate_limit": 1,
            "jitter_seconds": 0,
        }
        containment.request("PUT", "/sources", json=policy)
        source_context = self.context(source_id="public-source", newly_created=True)
        containment("read", source_context)
        with self.assertRaisesRegex(ContainmentError, "SOURCE_WRITE_STAGE_INVALID"):
            containment.request("PUT", "/sources", json=policy)
        enabled_source = self.api.sources[0]
        source_context["source_policy_sha256"] = hashlib.sha256(
            json.dumps(
                {key: enabled_source[key] for key in FINGERPRINT_FIELDS},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        containment("dispatch", source_context)
        containment.request(
            "POST",
            "/sources/public-source/collect",
            json={"profile_id": "profile", "request_id": "public-observation-owned"},
        )
        containment("poll", source_context)
        self.api.operations[0]["state"] = "COMPLETED"
        containment("cleanup", source_context)
        containment.request("PUT", "/sources", json={**policy, "enabled": False})
        containment("read", source_context)
        release = containment(
            "release", self.context(cleanup_status="RESTORED_DISABLED")
        )
        self.assertFalse(self.api.sources[0]["enabled"])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])
        self.assertTrue(release["released"])
        self.assertFalse(release["scheduler_contained"])
        self.assertFalse(release["source_policy_exclusive"])
        self.assertFalse(release["source_collection_exclusive"])
        self.assertNotIn("token", release)

    def _exercise_public_source_helper(self):
        self.api.collection_state = "COMPLETED"
        baseline = {
            "source": {"commit": REVISION},
            "profile_id": "profile",
            "application_id": "application",
            "operation_id": "baseline-operation",
            "request_id": "baseline-request",
            "profile_version": "v1",
            "selected_application_job_id": "fixture-job",
            "selected_application_route_id": "fixture-route",
            "confirmation": self.api.app["confirmation"],
            "external_submission_enabled": False,
        }
        containment = self.containment(baseline=baseline)
        evidence_path = self.run_dir / "public-source.json"
        evidence = run_public_source(
            "http://127.0.0.1:18732",
            baseline,
            evidence_path,
            acceptance_run_id="contained-run",
            request=containment.request,
            execution_scope="CONTROLLED_TRANSPORT",
            containment_preflight=containment,
            utc_now=lambda: STAMP,
        )
        self.assertEqual(evidence["result"], "PASS", evidence)
        self.assertEqual(evidence["cleanup"]["status"], "RESTORED_DISABLED")
        self.assertEqual(evidence["observed_real_job_ids"], ["real-job"])
        self.assertEqual(evidence["child_discovery"]["status"], "NOT_RUN")
        self.assertEqual(evidence["child_discovery"]["children"], [])
        self.assertEqual(evidence["child_discovery"]["lineage_sha256"], _digest([]))
        self.assertEqual(len(self.api.operations), 1)
        self.assertEqual(
            sum(path.endswith("/collect") for _, path, _ in self.api.calls), 1
        )
        self.assertFalse(self.api.sources[0]["enabled"])
        joined_context = self.context(
            acceptance_run_id="contained-run",
            source_id=evidence["source_id"],
            source_policy_sha256=evidence["source_policy_sha256"],
            newly_created=True,
            collection_operation_id=evidence["collection_operation_id"],
            collection_request_id=evidence["collection_request_id"],
            **child_containment_context(evidence),
        )
        containment("finalize", joined_context)
        released = containment(
            "release",
            {**joined_context, "cleanup_status": evidence["cleanup"]["status"]},
        )
        self.assertTrue(released["released"])
        self.assertFalse(released["source_policy_exclusive"])
        self.assertFalse(released["scheduler_contained"])
        self.assertFalse(released["source_collection_exclusive"])
        self.assertNotIn("token", released)
        self.assertEqual(self.docker.pause_calls, ["b" * 64])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])

    def test_public_source_helper_runs_through_containment_then_explicit_release(self):
        self._exercise_public_source_helper()

    def test_created_enabled_source_cannot_release_without_explicit_cleanup(self):
        containment = self.containment()
        context = self.context()
        containment("preflight", context)
        containment("enable", context)
        containment.request(
            "PUT",
            "/sources",
            json={
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
            },
        )
        containment("read", self.context(source_id="public-source", newly_created=True))
        with self.assertRaises(ContainmentError):
            containment("release", self.context(cleanup_status="RESTORED_DISABLED"))
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_source_appearing_without_this_helper_enable_is_not_adopted(self):
        containment = self.containment()
        context = self.context()
        containment("preflight", context)
        self.api.sources = [
            {
                "id": "other-source",
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
                "strategy": "FEED",
                "capabilities": {"direct_application": False},
            }
        ]
        with self.assertRaisesRegex(ContainmentError, "UNOWNED_SOURCE_APPEARED"):
            containment("read", context)
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.rows["scheduler"]["status"], "paused")
        self.assertEqual(self.docker.unpause_calls, [])

    def test_source_added_between_enable_preflight_and_write_is_not_overwritten(self):
        containment = self.containment()
        context = self.context()
        containment("preflight", context)
        containment("enable", context)
        self.api.sources = [
            {
                "id": "other-source",
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
                "strategy": "FEED",
                "capabilities": {"direct_application": False},
            }
        ]
        with self.assertRaisesRegex(ContainmentError, "SOURCE_ENABLE_SCOPE_INVALID"):
            containment.request(
                "PUT",
                "/sources",
                json={
                    "provider": "wwr",
                    "tenant": "default",
                    "enabled": True,
                    "poll_interval": 604800,
                    "concurrency": 1,
                    "request_timeout": 10,
                    "rate_limit": 1,
                    "jitter_seconds": 0,
                },
            )
        self.assertEqual(self.api.sources[0]["id"], "other-source")
        self.assertEqual(self.docker.unpause_calls, [])

    def test_policy_change_between_cleanup_preflight_and_disable_is_preserved(self):
        containment = self.containment()
        context = self.context()
        containment("preflight", context)
        containment("enable", context)
        containment.request(
            "PUT",
            "/sources",
            json={
                "provider": "wwr",
                "tenant": "default",
                "enabled": True,
                "poll_interval": 604800,
                "concurrency": 1,
                "request_timeout": 10,
                "rate_limit": 1,
                "jitter_seconds": 0,
            },
        )
        created = self.context(source_id="public-source", newly_created=True)
        containment("read", created)
        containment("cleanup", created)
        self.api.sources[0]["rate_limit"] = 7
        with self.assertRaisesRegex(ContainmentError, "SOURCE_DISABLE_SCOPE_INVALID"):
            containment.request(
                "PUT",
                "/sources",
                json={
                    "provider": "wwr",
                    "tenant": "default",
                    "enabled": False,
                    "poll_interval": 604800,
                    "concurrency": 1,
                    "request_timeout": 10,
                    "rate_limit": 1,
                    "jitter_seconds": 0,
                },
            )
        self.assertTrue(self.api.sources[0]["enabled"])
        self.assertEqual(self.api.sources[0]["rate_limit"], 7)
        self.assertEqual(self.docker.unpause_calls, [])

    def test_unapproved_api_mutations_are_rejected(self):
        containment = self.containment()
        containment("preflight", self.context())
        for method, path in (
            ("POST", "/applications/app/submit"),
            ("PUT", "/jobs/job"),
            ("DELETE", "/sources/public-source"),
        ):
            with (
                self.subTest(method=method, path=path),
                self.assertRaises(ContainmentError),
            ):
                containment.request(method, path)
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_collection_appearing_while_waiting_keeps_pause_and_claim(self):
        containment = self.containment()
        containment("preflight", self.context())

        def arrive(_seconds):
            self.clock[0] += _seconds
            self.now[0] += timedelta(seconds=_seconds)
            self.api.operations.append(
                {
                    "id": "late",
                    "kind": "DISCOVER",
                    "state": "RUNNING",
                    "entity_id": "other-profile",
                    "params": {"source_id": "other-source"},
                }
            )

        containment.sleep = arrive
        with self.assertRaisesRegex(ContainmentError, "ACTIVE_COLLECTION_EXISTS"):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.rows["scheduler"]["status"], "paused")

    def test_active_operation_inventory_cap_fails_closed(self):
        self.api.operations = [
            {"id": str(index), "kind": "OTHER", "state": "COMPLETED"}
            for index in range(100)
        ]
        containment = self.containment()
        with self.assertRaisesRegex(
            ContainmentError, "OPERATIONS_INVENTORY_INCOMPLETE"
        ):
            containment("preflight", self.context())
        self.assertEqual(self.docker.pause_calls, [])
        self.assertFalse(containment.claim_path.exists())

    def test_lost_lease_or_scheduler_identity_never_triggers_unpause(self):
        containment = self.containment()
        containment("preflight", self.context())
        self.now[0] += timedelta(seconds=17)
        with self.assertRaisesRegex(ContainmentError, "CONTAINMENT_LEASE_EXPIRED"):
            containment("poll", self.context())
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_release_requires_confirmed_cleanup_and_verified_baseline_then_waits_for_lease(
        self,
    ):
        containment = self.containment()
        containment("preflight", self.context())
        self.api.app["target"] = "EXTERNAL"
        with self.assertRaises(ContainmentError):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_release_waits_in_bounded_intervals_and_unpauses_only_owned_id(self):
        containment = self.containment()
        containment("preflight", self.context())
        containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.clock[0], 16.0)
        self.assertTrue(
            all(0 < interval <= 15 for interval in containment.sleep_intervals)
        )
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])
        self.assertFalse(containment.claim_path.exists())

    def _scheduler_health_lag(self):
        self.api.ready_status = 503
        self.api.ready["status"] = "degraded"
        self.api.ready["checks"]["scheduler"] = False
        for row in self.docker.rows.values():
            row["health"] = "unhealthy"

    def _full_health(self):
        self.api.ready_status = 200
        self.api.ready["status"] = "ready"
        self.api.ready["checks"]["scheduler"] = True
        for row in self.docker.rows.values():
            row["health"] = "healthy"

    def test_unhealthy_or_unknown_docker_health_is_denied_before_pause(self):
        for service in ("api", "scheduler"):
            for health in ("unhealthy", "starting", "missing", None, True, 1, {}):
                with self.subTest(service=service, health=health):
                    self.docker.rows[service]["health"] = health
                    containment = self.containment()
                    with self.assertRaisesRegex(ContainmentError, "PROJECT_CONTAINER_UNHEALTHY"):
                        containment("preflight", self.context())
                    self.assertFalse(containment.claim_path.exists())
                    self.assertEqual(self.docker.pause_calls, [])
                    self.docker.rows[service]["health"] = "healthy"

    def test_full_readiness_is_required_before_claim_or_pause(self):
        self.api.ready_status = 503
        self.api.ready["status"] = "degraded"
        self.api.ready["checks"]["scheduler"] = False
        containment = self.containment()
        with self.assertRaisesRegex(ContainmentError, "FIXTURE_API_NOT_READY"):
            containment("preflight", self.context())
        self.assertFalse(containment.claim_path.exists())
        self.assertEqual(self.docker.pause_calls, [])

    def test_owned_pause_accepts_only_healthy_or_unhealthy_docker_health(self):
        containment = self.containment()
        self.docker.on_pause = self._scheduler_health_lag
        self.assertTrue(containment("preflight", self.context())["scheduler_contained"])
        for service in ("api", "scheduler"):
            for health in ("healthy", "unhealthy"):
                self.docker.rows[service]["health"] = health
                self.assertTrue(containment("poll", self.context())["scheduler_contained"])
            for health in ("starting", "missing", None, False, 1, []):
                with self.subTest(service=service, health=health):
                    self.docker.rows[service]["health"] = health
                    with self.assertRaisesRegex(ContainmentError, "PROJECT_CONTAINER_UNHEALTHY"):
                        containment("poll", self.context())
                    self.assertTrue(containment.claim_path.exists())
                    self.assertEqual(self.docker.unpause_calls, [])
            self.docker.rows[service]["health"] = "unhealthy"

    def test_owned_pause_requires_strict_liveness_and_fixture_mode(self):
        containment = self.containment()
        containment("preflight", self.context())
        original = deepcopy(self.api.health)
        for key, value in (
            ("status", "degraded"), ("check", "readiness"), ("mode", "REMOTE"),
            ("operation_mode", "PERSONAL"), ("external_submission_enabled", 0),
            ("external_submission_enabled", True), ("unexpected", True),
        ):
            with self.subTest(key=key, value=value):
                self.api.health = {**original, key: value}
                with self.assertRaises(ContainmentError):
                    containment("poll", self.context())
        for key in original:
            with self.subTest(missing=key):
                self.api.health = {k: v for k, v in original.items() if k != key}
                with self.assertRaises(ContainmentError):
                    containment("poll", self.context())
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, [])

    def test_each_non_scheduler_dependency_and_exact_worker_is_required(self):
        containment = self.containment()
        self.docker.on_pause = self._scheduler_health_lag
        containment("preflight", self.context())
        for key in sorted(READINESS_DEPENDENCIES):
            with self.subTest(dependency=key):
                self.api.ready["checks"][key] = False
                with self.assertRaisesRegex(ContainmentError, "FIXTURE_API_DEPENDENCY_UNHEALTHY"):
                    containment("poll", self.context())
                self.api.ready["checks"][key] = True
        for queue in sorted(READINESS_WORKERS):
            with self.subTest(queue=queue):
                self.api.ready["checks"]["workers"][queue] = False
                with self.assertRaisesRegex(ContainmentError, "FIXTURE_API_DEPENDENCY_UNHEALTHY"):
                    containment("poll", self.context())
                self.api.ready["checks"]["workers"][queue] = True
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, [])

    def test_readiness_booleans_keys_status_and_backend_are_strict(self):
        containment = self.containment()
        containment("preflight", self.context())
        original = deepcopy(self.api.ready)
        cases = []
        for key in sorted(READINESS_DEPENDENCIES | {"scheduler"}):
            for value in (1, "true", None):
                body = deepcopy(original)
                body["checks"][key] = value
                cases.append((200, body))
            body = deepcopy(original)
            del body["checks"][key]
            cases.append((200, body))
        for queue in sorted(READINESS_WORKERS):
            body = deepcopy(original)
            body["checks"]["workers"][queue] = 1
            cases.append((200, body))
            body = deepcopy(original)
            del body["checks"]["workers"][queue]
            cases.append((200, body))
        for checks in (None, [], {**original["checks"], "unexpected": True}):
            cases.append((200, {**original, "checks": checks}))
        for workers in ([], {**original["checks"]["workers"], "extra-queue": True}):
            body = deepcopy(original)
            body["checks"]["workers"] = workers
            cases.append((200, body))
        for key in ("status", "db_backend", "checks"):
            cases.append((200, {k: v for k, v in original.items() if k != key}))
        cases.extend((
            (200, {**original, "db_backend": "sqlite"}),
            (200, {**original, "unexpected": True}),
            (200, {**original, "status": "degraded"}),
            (503, original),
            (201, original),
        ))
        for status, scheduler, label in ((200, False, "degraded"), (503, True, "degraded"),
                                         (200, False, "ready"), (503, False, "ready")):
            body = deepcopy(original)
            body["status"] = label
            body["checks"]["scheduler"] = scheduler
            cases.append((status, body))
        for index, (status, body) in enumerate(cases):
            with self.subTest(case=index):
                self.api.ready_status, self.api.ready = status, body
                with self.assertRaisesRegex(ContainmentError, "FIXTURE_API_READINESS_INVALID"):
                    containment("poll", self.context())
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.unpause_calls, [])

    def test_actual_http_503_body_is_decoded_only_for_readiness(self):
        containment = self.containment()
        containment._injected_request = None
        self.docker.on_pause = self._scheduler_health_lag
        real_client = httpx.Client
        failing_path = [None]
        statuses = []

        def handle(request):
            path = request.url.path
            if path == failing_path[0]:
                return httpx.Response(503, json={"status": "degraded"})
            body = self.api.request(request.method, path)
            status = 200
            if isinstance(body, httpx.Response):
                status, body = body.status_code, body.json()
            statuses.append((path, status))
            return httpx.Response(status, json=body)

        def client(**kwargs):
            self.assertEqual(kwargs, {
                "base_url": "http://127.0.0.1:18732", "timeout": API_TIMEOUT_SECONDS,
                "trust_env": False, "follow_redirects": False,
            })
            return real_client(transport=httpx.MockTransport(handle), **kwargs)

        with patch("cli.acceptance_containment.httpx.Client", side_effect=client):
            proof = containment("preflight", self.context())
            self.assertTrue(proof["scheduler_contained"])
            self.assertIn(("/ready", 200), statuses)
            self.assertIn(("/ready", 503), statuses)
            for path in ("/health", "/settings", "/sources", "/readiness"):
                with self.subTest(path=path):
                    failing_path[0] = path
                    with self.assertRaisesRegex(ContainmentError, "LOOPBACK_API_REQUEST_FAILED"):
                        containment.request("GET", path)
            failing_path[0] = None
            self.docker.on_unpause = self._full_health
            self.assertTrue(containment("release", self.context(cleanup_status="NOT_REQUIRED"))["released"])
        self.assertFalse(containment.claim_path.exists())

    def test_readiness_non_503_errors_and_invalid_json_fail_closed(self):
        containment = self.containment()
        containment("preflight", self.context())
        for status, body in ((500, b'{}'), (302, b'{}'), (503, b'not-json')):
            def request(method, path, **kwargs):
                if path == "/ready":
                    return httpx.Response(status, content=body,
                        request=httpx.Request("GET", "http://127.0.0.1:18732/ready"))
                return self.api.request(method, path, **kwargs)
            containment._injected_request = request
            with self.subTest(status=status, body=body):
                with self.assertRaisesRegex(ContainmentError, "LOOPBACK_API_REQUEST_FAILED"):
                    containment("poll", self.context())
        self.assertTrue(containment.claim_path.exists())

    def test_health_exception_requires_exact_claim_and_owned_pause(self):
        containment = self.containment()
        self.docker.on_pause = self._scheduler_health_lag
        containment("preflight", self.context())
        original = containment.claim_path.read_text(encoding="utf-8")
        for key in ("owner", "token", "project", "api_container_id", "scheduler_container_id"):
            with self.subTest(key=key):
                claim = json.loads(original)
                claim[key] = "foreign"
                containment.claim_path.write_text(json.dumps(claim), encoding="utf-8")
                with self.assertRaisesRegex(ContainmentError, "CONTAINMENT_OWNERSHIP_CHANGED"):
                    containment("poll", self.context())
                containment.claim_path.write_text(original, encoding="utf-8")
        for key, value in (("_admitted", False), ("_paused_by_us", False),
                           ("_original_scheduler_status", "paused")):
            with self.subTest(flag=key):
                old = getattr(containment, key)
                setattr(containment, key, value)
                with self.assertRaisesRegex(ContainmentError, "OWNED_SCHEDULER_NOT_PAUSED"):
                    containment._verify_targets()
                setattr(containment, key, old)
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_unhealthy_owned_pause_still_checks_runtime_identity_and_state(self):
        containment = self.containment()
        self.docker.on_pause = self._scheduler_health_lag
        containment("preflight", self.context())
        original = deepcopy(self.docker.rows)
        for service in ("api", "scheduler"):
            for key, value in (("image_id", "sha256:" + "f" * 64), ("image_ref", "foreign"),
                               ("working_dir", "/foreign"), ("labels", {}),
                               ("status", "exited"), ("paused", service == "api")):
                with self.subTest(service=service, key=key):
                    self.docker.rows[service][key] = value
                    with self.assertRaises(ContainmentError):
                        containment("poll", self.context())
                    self.docker.rows = deepcopy(original)
        self.docker.rows["api"]["ports"]["8000/tcp"][0]["HostIp"] = "0.0.0.0"
        with self.assertRaisesRegex(ContainmentError, "PROJECT_API_BINDING_CHANGED"):
            containment("poll", self.context())
        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_release_retains_expired_claim_until_real_readiness_and_both_health_checks_return(self):
        containment = self.containment()
        self.docker.on_pause = self._scheduler_health_lag
        containment("preflight", self.context())
        self.docker.on_unpause = self._scheduler_health_lag
        restoration_sleeps = []

        def recover(seconds):
            self._sleep(seconds)
            if self.docker.unpause_calls:
                restoration_sleeps.append(seconds)
                claim = json.loads(containment.claim_path.read_text(encoding="utf-8"))
                self.assertLessEqual(datetime.fromisoformat(claim["lease_expires_at"]), self.now[0])
                self.assertFalse(self.docker.rows["scheduler"]["paused"])
                if len(restoration_sleeps) == 1:
                    self.api.ready_status = 200
                    self.api.ready["status"] = "ready"
                    self.api.ready["checks"]["scheduler"] = True
                    self.docker.rows["scheduler"]["health"] = "healthy"
                elif len(restoration_sleeps) == 2:
                    self.docker.rows["api"]["health"] = "healthy"

        containment.sleep = recover
        released = containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertTrue(released["released"])
        self.assertEqual(restoration_sleeps, [5, 5])
        self.assertEqual(self.clock[0], 26)
        self.assertFalse(containment.claim_path.exists())
        self.assertEqual(self.docker.pause_calls, ["b" * 64])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])

    def test_release_restoration_times_out_repauses_exact_id_and_preserves_claim(self):
        containment = self.containment()
        self.docker.on_pause = self._scheduler_health_lag
        containment("preflight", self.context())
        self.docker.on_unpause = self._scheduler_health_lag
        with self.assertRaisesRegex(ContainmentError, "PROJECT_HEALTH_RESTORE_TIMEOUT"):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.clock[0], 16 + HEALTH_RESTORE_TIMEOUT_SECONDS)
        self.assertTrue(containment.claim_path.exists())
        self.assertFalse(containment._released)
        self.assertTrue(self.docker.rows["scheduler"]["paused"])
        self.assertEqual(self.docker.pause_calls, ["b" * 64, "b" * 64])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])

    def test_release_attempt_ceiling_is_independent_of_wall_clock_and_monotonic_rollback(self):
        containment = self.containment()
        containment("preflight", self.context())
        self.docker.on_unpause = self._scheduler_health_lag
        restoration_sleeps = []
        containment.monotonic = lambda: 0

        def stalled_clock(seconds):
            if self.docker.unpause_calls:
                restoration_sleeps.append(seconds)
                # Wall time is frozen at the intentionally expired lease.
            else:
                self._sleep(seconds)

        containment.sleep = stalled_clock
        with self.assertRaisesRegex(ContainmentError, "PROJECT_HEALTH_RESTORE_TIMEOUT"):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(sum(restoration_sleeps), HEALTH_RESTORE_TIMEOUT_SECONDS)
        self.assertTrue(containment.claim_path.exists())
        self.assertTrue(self.docker.rows["scheduler"]["paused"])

    def test_non_scheduler_readiness_failure_after_unpause_aborts_without_retry(self):
        containment = self.containment()
        containment("preflight", self.context())

        def unrelated_failure():
            self._scheduler_health_lag()
            self.api.ready["checks"]["database"] = False

        self.docker.on_unpause = unrelated_failure
        with self.assertRaisesRegex(ContainmentError, "FIXTURE_API_DEPENDENCY_UNHEALTHY"):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.clock[0], 16)
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.pause_calls, ["b" * 64, "b" * 64])

    def test_claim_drift_after_unpause_is_preserved_and_only_owned_scheduler_repaused(self):
        containment = self.containment()
        containment("preflight", self.context())

        def claim_drift():
            claim = json.loads(containment.claim_path.read_text(encoding="utf-8"))
            claim["token"] = "changed"
            containment.claim_path.write_text(json.dumps(claim), encoding="utf-8")

        self.docker.on_unpause = claim_drift
        with self.assertRaisesRegex(ContainmentError, "CONTAINMENT_OWNERSHIP_CHANGED"):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.clock[0], 16)
        self.assertEqual(json.loads(containment.claim_path.read_text())["token"], "changed")
        self.assertEqual(self.docker.pause_calls, ["b" * 64, "b" * 64])

    def test_replaced_scheduler_after_unpause_is_never_repaused_or_claim_deleted(self):
        containment = self.containment()
        containment("preflight", self.context())
        self.docker.on_unpause = lambda: self.docker.containers.update(scheduler="c" * 64)
        with self.assertRaisesRegex(ContainmentError, "PROJECT_CONTAINER_REPLACED"):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.clock[0], 16)
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.pause_calls, ["b" * 64])
        self.assertEqual(self.docker.unpause_calls, ["b" * 64])

    def test_api_identity_drift_after_unpause_repauses_owned_scheduler(self):
        containment = self.containment()
        containment("preflight", self.context())
        self.docker.on_unpause = lambda: self.docker.rows["api"].update(image_id="sha256:" + "f" * 64)
        with self.assertRaisesRegex(ContainmentError, "PROJECT_IMAGE_CHANGED"):
            containment("release", self.context(cleanup_status="NOT_REQUIRED"))
        self.assertEqual(self.clock[0], 16)
        self.assertTrue(containment.claim_path.exists())
        self.assertEqual(self.docker.pause_calls, ["b" * 64, "b" * 64])


if __name__ == "__main__":
    unittest.main()
