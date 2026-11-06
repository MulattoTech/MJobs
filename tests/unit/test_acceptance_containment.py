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

from cli.acceptance_containment import (
    CONTAINER_INSPECT_TEMPLATE,
    FINGERPRINT_FIELDS,
    ContainmentError,
    DockerCLI,
    OwnedPublicContainment,
)
from cli.acceptance_public_source import run as run_public_source
from services.discovery.sources.remotive import Remotive

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


def additional_source_record(source_id, *, enabled=True):
    return {
        "id": source_id,
        "provider": "remotive",
        "tenant": "default",
        "enabled": enabled,
        "poll_interval": 604800,
        "concurrency": 1,
        "request_timeout": 10,
        "rate_limit": 1,
        "jitter_seconds": 0,
        "strategy": Remotive(
            request_timeout=10, rate_limit=1, concurrency=1
        ).capabilities().strategy.value,
        "capabilities": Remotive(
            request_timeout=10, rate_limit=1, concurrency=1
        ).capabilities().as_dict(),
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
        self.on_pause()

    def unpause(self, container_id):
        self.unpause_calls.append(container_id)
        self.rows["scheduler"]["paused"] = False
        self.rows["scheduler"]["status"] = "running"


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.sources = []
        self.operations = []
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
            return {"external_submission_enabled": False}
        if (method, path) == ("GET", "/operations"):
            return {"operations": deepcopy(self.operations)}
        if (method, path) == ("GET", "/sources"):
            return {"sources": deepcopy(self.sources)}
        if (method, path) == ("PUT", "/sources"):
            if not self.sources:
                self.sources = [
                    {
                        "id": "public-source",
                        "strategy": "FEED",
                        "capabilities": {"direct_application": False},
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
        return OwnedPublicContainment(
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

    def test_additional_remotive_source_is_bound_created_disabled_and_released(self):
        containment = self.containment()
        primary = self.context()
        containment("preflight", primary)

        proof = containment.bind_additional_source(
            policy=ADDITIONAL_POLICY, primary_context=primary
        )

        self.assertTrue(proof["additional_source_absent"])
        self.assertEqual(proof["provider"], "remotive")
        absent_context = {
            **primary,
            "additional_source": {
                "provider": "remotive",
                "tenant": "default",
                "source_id": None,
                "enabled_policy_sha256": None,
                "disabled_policy_sha256": None,
                "enabled": False,
            },
        }
        containment("read", absent_context)
        containment.additional_source("check", primary)
        enabled_source = additional_source_record("remotive-source")
        self.api.sources = [enabled_source]
        enabled_hash = containment.policy_sha256(enabled_source)
        enabled_context = {
            **primary,
            "additional_source_id": "remotive-source",
            "additional_enabled_policy_sha256": enabled_hash,
        }
        created = containment.additional_source("created", enabled_context)
        self.assertTrue(created["additional_source_policy_verified"])
        self.assertEqual(created["additional_source_id"], "remotive-source")

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
            "provider": "remotive",
            "tenant": "default",
            "source_id": "remotive-source",
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

    def test_additional_source_race_is_not_adopted_or_disabled(self):
        containment = self.containment()
        primary = self.context()
        containment("preflight", primary)
        containment.bind_additional_source(
            policy=ADDITIONAL_POLICY, primary_context=primary
        )
        self.api.sources = [additional_source_record("competing-source")]
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
        containment = self.containment()
        primary = self.context()
        containment("preflight", primary)
        containment.bind_additional_source(
            policy=ADDITIONAL_POLICY, primary_context=primary
        )
        source = additional_source_record("remotive-source")
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
        containment = self.containment()
        primary = self.context()
        containment("preflight", primary)
        containment.bind_additional_source(
            policy=ADDITIONAL_POLICY, primary_context=primary
        )

        with self.assertRaisesRegex(
            ContainmentError, "ADDITIONAL_SOURCE_CONTEXT_REQUIRED"
        ):
            containment("read", primary)

        self.assertEqual(self.docker.unpause_calls, [])
        self.assertTrue(containment.claim_path.exists())

    def test_source_appearing_before_latched_create_fails_closed(self):
        containment = self.containment()
        primary = self.context()
        containment("preflight", primary)
        containment.bind_additional_source(
            policy=ADDITIONAL_POLICY, primary_context=primary
        )
        self.api.sources = [additional_source_record("unexpected-source")]
        absent_context = {
            **primary,
            "additional_source": {
                "provider": "remotive",
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

    def test_public_source_helper_runs_through_containment_then_explicit_release(self):
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


if __name__ == "__main__":
    unittest.main()
