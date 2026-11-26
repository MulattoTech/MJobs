"""No-Docker safety tests for the opt-in disposable fixture lifecycle."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from services.applications.browser_profiles import configured_browser_profile
from tools import dev_fixture_guard as guard


class FixtureGuardTests(TestCase):
    PROJECT = "mjobs-fixture-012345abcdef"
    TOKEN = "a" * 32
    LEASE = "9999999999"
    IMAGE_REF = "mjobs-req292:sha-1ee9f4f4"
    IMAGE_ID = "sha256:" + "c" * 64
    DOCKER_CONTEXT = "desktop-linux"
    DOCKER_ENDPOINT = "npipe:////./pipe/dockerdesktoplinuxengine"

    def recovery_environment(self, expiry=1234567890, port=18741):
        return guard.fixture_environment(
            self.PROJECT, self.TOKEN, expiry, port, recovery_mode=True,
            image_ref=self.IMAGE_REF, image_id=self.IMAGE_ID,
            docker_context=self.DOCKER_CONTEXT, docker_endpoint=self.DOCKER_ENDPOINT)

    def write_recovery_dotenv(self, root, expiry=1234567890):
        env = self.recovery_environment(expiry=expiry)
        path = guard.recovery_environment_file(
            self.PROJECT, self.TOKEN, expiry, environment=env)
        return env, path

    def test_recovery_browser_profile_validates_in_controlled_process_and_dotenv(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp)), \
                patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "personal-profile"}):
            env, dotenv = self.write_recovery_dotenv(Path(temp))
            for controlled_env in (env, guard.read_recovery_dotenv(dotenv)):
                with patch.dict(os.environ, controlled_env, clear=True):
                    self.assertEqual(configured_browser_profile(), "default")

    def labelled(self, **overrides):
        row = {
            guard.PROJECT_LABEL: self.PROJECT,
            guard.TOKEN_LABEL: self.TOKEN,
            guard.EXPIRY_LABEL: self.LEASE,
            guard.SERVICE_LABEL: "api",
        }
        row.update(overrides)
        return row

    def recovery_labelled(self, **overrides):
        return self.labelled(**{guard.MODE_LABEL: guard.RECOVERY_MODE}, **overrides)

    def recovery_resources(self, **overrides):
        labels = {guard.PROJECT_LABEL: self.PROJECT, guard.TOKEN_LABEL: self.TOKEN,
                  guard.EXPIRY_LABEL: self.LEASE, guard.MODE_LABEL: guard.RECOVERY_MODE}
        labels.update(overrides)
        return ([{"Name": f"{self.PROJECT}_{name}", "Labels": dict(labels)}
                 for name in guard.VOLUMES],
                [{"Name": f"{self.PROJECT}_{name}", "Labels": dict(labels)}
                 for name in guard.NETWORKS])

    def test_reject_unowned_container_before_destructive_command(self):
        with patch.object(guard, "containers", return_value=[
            self.labelled(), self.labelled(**{guard.TOKEN_LABEL: "other"}),
        ]), patch.object(guard, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "unowned container"):
                guard.verify_owned(self.PROJECT, self.TOKEN)
            docker.assert_not_called()

    def test_reject_non_fixture_project_even_with_matching_token(self):
        with patch.object(guard, "containers") as containers:
            with self.assertRaises(ValueError):
                guard.verify_owned("mjobs-micro", self.TOKEN)
            containers.assert_not_called()

    def test_cleanup_only_verified_identity_and_no_global_prune(self):
        calls = []
        def fake_docker(*args, **kwargs):
            calls.append((args, kwargs))
            return ""
        with patch.object(guard, "containers", return_value=[self.labelled()]), \
             patch.object(guard, "docker", side_effect=fake_docker):
            guard.cleanup(self.PROJECT, self.TOKEN)
        action = calls[-1]
        self.assertIn("--project-name", action[0])
        self.assertIn(self.PROJECT, action[0])
        self.assertIn("down", action[0])
        self.assertIn("--volumes", action[0])
        self.assertNotIn("prune", action[0])
        self.assertEqual(action[1]["env"]["MJOBS_DEV_GUARD_TOKEN"], self.TOKEN)
        self.assertEqual(action[1]["env"]["MJOBS_DEV_GUARD_EXPIRES"], self.LEASE)

    def test_reject_unexpected_volume_before_down(self):
        def fake_docker(*args, **kwargs):
            return "otherproject_database" if args[:2] == ("volume", "ls") else ""
        with patch.object(guard, "containers", return_value=[self.labelled()]), \
             patch.object(guard, "docker", side_effect=fake_docker):
            with self.assertRaisesRegex(RuntimeError, "unexpected project volume"):
                guard.cleanup(self.PROJECT, self.TOKEN)

    def test_expired_sweep_ignores_legacy_and_future(self):
        rows = [
            {"project": "mjobs-fixture-111111111111", "managed": False,
             "expires": None, "token": None, "containers": 3},
            {"project": self.PROJECT, "managed": True,
             "expires": "1", "token": self.TOKEN, "containers": 4},
            {"project": "mjobs-fixture-222222222222", "managed": True,
             "expires": self.LEASE, "token": "b" * 32, "containers": 3},
        ]
        with patch.object(guard, "inventory", return_value=rows), \
             patch.object(guard, "recovery_resource_inventory", return_value=[]), \
             patch.object(guard, "cleanup") as cleanup:
            guard.sweep(apply=False)
            cleanup.assert_not_called()
            guard.sweep(apply=True)
            cleanup.assert_called_once_with(self.PROJECT, self.TOKEN)

    def test_environment_is_synthetic_and_drops_other_compose_files(self):
        with patch.dict(os.environ, {"COMPOSE_FILE": "danger.yml",
                                     "POSTGRES_PASSWORD": "production-pass"}):
            env = guard.fixture_environment(self.PROJECT, self.TOKEN, 1234567890, 18741)
        self.assertNotIn("COMPOSE_FILE", env)
        self.assertTrue(env["POSTGRES_PASSWORD"] != "production-pass")
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], self.PROJECT)
        self.assertEqual(env["MJOBS_OPERATION_MODE"], "FIXTURE")
        self.assertEqual(env["MJOBS_COMPOSE_MOCK"], "1")

    def test_recovery_mode_uses_only_fixed_root_first_compose_file_list(self):
        expected = tuple(str(path.resolve()) for path in (
            guard.COMPOSE, guard.OVERRIDE, guard.RECOVERY_OVERRIDE))
        default = guard.compose_args(self.PROJECT)
        recovery = guard.compose_args(self.PROJECT, recovery_mode=True)
        default_files = tuple(default[index + 1] for index, item in enumerate(default[:-1])
                              if item == "--file")
        recovery_files = tuple(recovery[index + 1] for index, item in enumerate(recovery[:-1])
                               if item == "--file")
        self.assertEqual(default_files, expected[:2])
        self.assertEqual(recovery_files, expected)
        self.assertEqual(recovery_files[0], str(guard.COMPOSE.resolve()))
        self.assertNotIn("--env-file", default)

    def test_recovery_child_environment_gets_exact_compose_list_and_fixture_mode(self):
        expected = os.pathsep.join(guard.compose_files(recovery_mode=True))
        with patch.dict(os.environ, {"COMPOSE_FILE": "foreign-compose.yml",
                                     "POSTGRES_PASSWORD": "production-pass"}):
            env = self.recovery_environment()
        self.assertEqual(env["COMPOSE_FILE"], expected)
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], self.PROJECT)
        self.assertEqual(env["MJOBS_OPERATION_MODE"], "FIXTURE")
        self.assertEqual(env["MJOBS_COMPOSE_MOCK"], "1")
        self.assertNotEqual(env["POSTGRES_PASSWORD"], "production-pass")
        self.assertEqual(env["MJOBS_EXTERNAL_SUBMISSIONS_ENABLED"], "0")
        self.assertEqual(env["MJOBS_OPENAI_API_KEY"], "")
        self.assertEqual(env["MJOBS_OPENAI_BASE_URL"], "http://127.0.0.1:9/v1")
        self.assertEqual(env.get("OPENAI_API_KEY"), "")
        self.assertEqual(env["MJOBS_IMAGE"], self.IMAGE_ID)
        self.assertEqual(env["MJOBS_RECOVERY_IMAGE_REF"], self.IMAGE_REF)
        self.assertEqual(env["MJOBS_RECOVERY_IMAGE_ID"], self.IMAGE_ID)
        self.assertEqual(env["MJOBS_RECOVERY_DOCKER_CONTEXT"], self.DOCKER_CONTEXT)

    def test_recovery_environment_drops_poisoned_host_values_and_uses_controlled_dotenv(self):
        secret = "POISON-SECRET-MUST-NOT-ESCAPE"
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp)), \
                patch.dict(os.environ, {
                    "COMPOSE_FILE": "foreign-compose.yml",
                    "COMPOSE_ENV_FILES": "foreign.env",
                    "OPENAI_API_KEY": secret,
                    "AZURE_OPENAI_API_KEY": secret,
                    "POSTGRES_PASSWORD": "POISON_DB_PASSWORD",
                    "MJOBS_CREDENTIAL_ENCRYPTION_KEY": "POISON_VAULT_KEY",
                    "MJOBS_OPENAI_API_KEY": secret,
                    "MJOBS_OPENAI_BASE_URL": "https://production.invalid/v1",
                    "MJOBS_PUBLIC_HOST": "production.invalid",
                    "MJOBS_EXTERNAL_SUBMISSIONS_ENABLED": "1",
                    "HTTPS_PROXY": "http://proxy.invalid:8080",
                    "DOCKER_CONTEXT": "default",
                    "DOCKER_HOST": "ssh://remote.invalid",
                }, clear=False):
            env = self.recovery_environment()
            path = guard.recovery_environment_file(self.PROJECT, self.TOKEN, 1234567890,
                                                   environment=env)
            text = path.read_text(encoding="utf-8")
            recovery_args = guard.compose_args(self.PROJECT, recovery_mode=True)
        self.assertEqual(env["DOCKER_HOST"], self.DOCKER_ENDPOINT)
        self.assertNotIn("DOCKER_CONTEXT", env)
        self.assertEqual(env["COMPOSE_FILE"], os.pathsep.join(guard.compose_files(True)))
        self.assertTrue("HTTPS_PROXY" not in env)
        self.assertEqual(env.get("OPENAI_API_KEY"), "")
        self.assertEqual(env["MJOBS_OPENAI_API_KEY"], "")
        self.assertEqual(env["MJOBS_OPENAI_BASE_URL"], "http://127.0.0.1:9/v1")
        self.assertEqual(env["MJOBS_PUBLIC_HOST"], "")
        self.assertEqual(env["MJOBS_EXTERNAL_SUBMISSIONS_ENABLED"], "0")
        self.assertTrue(secret not in text)
        self.assertTrue("production.invalid" not in text)
        env_file_index = recovery_args.index("--env-file")
        self.assertEqual(recovery_args[env_file_index + 1], str(path))
        self.assertEqual(env["COMPOSE_ENV_FILES"], str(path))

    def test_recovery_rejects_poisoned_controlled_dotenv_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp)):
            env = self.recovery_environment()
            path = guard.recovery_environment_file(self.PROJECT, self.TOKEN, 1234567890,
                                                   environment=env)
            content = path.read_text(encoding="utf-8").replace(
                "MJOBS_OPENAI_API_KEY=", "MJOBS_OPENAI_API_KEY=DOTENV-POISON")
            content = content.replace("POSTGRES_PASSWORD=", "POSTGRES_PASSWORD=DOTENV-DB-POISON")
            content = content.replace("MJOBS_OPENAI_BASE_URL=http://127.0.0.1:9/v1",
                                      "MJOBS_OPENAI_BASE_URL=https://production.invalid/v1")
            content = content.replace(f"MJOBS_RECOVERY_IMAGE_ID={self.IMAGE_ID}",
                                      "MJOBS_RECOVERY_IMAGE_ID=sha256:" + "d" * 64)
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "dotenv identity is invalid") as error:
                guard.recovery_environment_file(self.PROJECT, self.TOKEN, 1234567890,
                                                environment=env)
            self.assertNotIn("DOTENV-POISON", str(error.exception))

    def test_docker_failure_omits_secret_stderr(self):
        secret = "DOCKER-STDERR-SECRET"
        with patch.object(guard.subprocess, "run", return_value=Mock(
                returncode=1, stderr=f"failed with {secret}")):
            with self.assertRaises(RuntimeError) as error:
                guard.docker("compose", "config")
        self.assertNotIn(secret, str(error.exception))

    def test_remote_and_ambiguous_docker_endpoints_are_rejected(self):
        for endpoint in ("tcp://engine.example:2376", "ssh://engine.example",
                         "tcp://user:pass@127.0.0.1:2376", "tcp://127.0.0.1:2376/path",
                         " tcp://127.0.0.1:2376"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, "endpoint"):
                    guard.normalize_local_docker_endpoint(endpoint)

    def test_context_validation_rejects_remote_endpoint_before_mutation(self):
        calls = []

        def fake_docker(*args, **kwargs):
            calls.append(args)
            return "desktop-linux" if args[:2] == ("context", "show") else "tcp://remote.invalid:2376"

        with patch.object(guard, "docker", side_effect=fake_docker):
            with self.assertRaisesRegex(ValueError, "local engine endpoint"):
                guard.validate_local_docker_context(self.DOCKER_CONTEXT)
        self.assertEqual(calls, [("context", "show"),
                                 ("context", "inspect", self.DOCKER_CONTEXT,
                                  "--format", "{{.Endpoints.docker.Host}}")])

    def test_context_discovery_returns_socket_binding_without_context_override(self):
        poisoned = {"DOCKER_CONTEXT": "other", "DOCKER_HOST": "ssh://remote.invalid",
                    "DOCKER_TLS_VERIFY": "1"}
        with patch.object(guard, "docker", side_effect=[
                self.DOCKER_CONTEXT, "npipe:////./pipe/dockerDesktopLinuxEngine"]) as docker:
            context, endpoint, env = guard.validate_local_docker_context(
                self.DOCKER_CONTEXT, environment=poisoned)
        self.assertEqual(context, self.DOCKER_CONTEXT)
        self.assertEqual(endpoint, self.DOCKER_ENDPOINT)
        self.assertEqual(env, {"DOCKER_HOST": self.DOCKER_ENDPOINT})
        for call in docker.call_args_list:
            self.assertEqual(call.kwargs["env"], {"DOCKER_CONTEXT": self.DOCKER_CONTEXT})

    def test_image_admission_rejects_stale_tag_and_missing_immutable_id(self):
        with patch.object(guard, "docker", side_effect=[self.IMAGE_ID, "sha256:" + "d" * 64]):
            with self.assertRaisesRegex(RuntimeError, "tag does not match"):
                guard.validate_local_campaign_image(
                    self.IMAGE_REF, self.IMAGE_ID, {"DOCKER_HOST": self.DOCKER_ENDPOINT})
        with patch.object(guard, "docker", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "ID is not present locally"):
                guard.validate_local_campaign_image(
                    self.IMAGE_REF, self.IMAGE_ID, {"DOCKER_HOST": self.DOCKER_ENDPOINT})

    def test_image_admission_rejects_invalid_ref_or_digest_without_docker(self):
        with patch.object(guard, "docker") as docker:
            for image_ref, image_id in (("latest", self.IMAGE_ID),
                                        (self.IMAGE_REF, "sha256:not-a-digest")):
                with self.subTest(image_ref=image_ref, image_id=image_id):
                    with self.assertRaises(ValueError):
                        guard.validate_local_campaign_image(
                            image_ref, image_id, {"DOCKER_HOST": self.DOCKER_ENDPOINT})
            docker.assert_not_called()

    def test_recovery_run_uses_same_mode_for_start_child_watchdog_and_failure_cleanup(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        args = argparse.Namespace(ttl_minutes=60, max_fixtures=1, port=18741,
                                  build=False, recovery=True,
                                  image_ref=self.IMAGE_REF, image_id=self.IMAGE_ID,
                                  docker_context=self.DOCKER_CONTEXT,
                                  command=["--", "python", "-m", "cli.compose_recovery"])
        watchdog = Mock()
        watchdog.poll.return_value = None
        child = Mock(returncode=7)
        docker_calls = []
        def fake_docker(*command, **kwargs):
            docker_calls.append((command, kwargs))
            return ""
        with patch.object(guard, "sweep") as sweep, \
             patch.object(guard, "validate_local_docker_context",
                          return_value=(self.DOCKER_CONTEXT, self.DOCKER_ENDPOINT,
                                        {"DOCKER_HOST": self.DOCKER_ENDPOINT})), \
             patch.object(guard, "validate_local_campaign_image", return_value=self.IMAGE_ID), \
             patch.object(guard, "containers", return_value=[]), \
             patch.object(guard, "project_resources", return_value={
                 "containers": [], "volumes": [], "networks": []}), \
             patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp.name)), \
             patch.object(guard, "spawn_watchdog", return_value=watchdog) as spawn, \
             patch.object(guard, "docker", side_effect=fake_docker), \
             patch.object(guard, "cleanup") as cleanup, \
             patch.object(guard.subprocess, "run", return_value=child) as run_child:
            code = guard.run(args)
        self.assertEqual(code, 7)
        sweep.assert_not_called()
        project = next(kwargs["env"]["COMPOSE_PROJECT_NAME"]
                       for _, kwargs in docker_calls if "COMPOSE_PROJECT_NAME" in kwargs["env"])
        child_env = run_child.call_args.kwargs["env"]
        expected_files = os.pathsep.join(guard.compose_files(recovery_mode=True))
        self.assertEqual(child_env["COMPOSE_FILE"], expected_files)
        up, up_kwargs = next((call, kwargs) for call, kwargs in docker_calls if "up" in call)
        self.assertEqual(tuple(up[index + 1] for index, item in enumerate(up[:-1]) if item == "--file"),
                         guard.compose_files(recovery_mode=True))
        self.assertEqual(up_kwargs["env"]["COMPOSE_FILE"], expected_files)
        self.assertIn("--no-build", up)
        self.assertNotIn("--build", up)
        for environment in (child_env, up_kwargs["env"], cleanup.call_args.kwargs["env"]):
            self.assertEqual(environment["DOCKER_HOST"], self.DOCKER_ENDPOINT)
            self.assertNotIn("DOCKER_CONTEXT", environment)
        self.assertEqual(spawn.call_args.args[0], project)
        self.assertTrue(spawn.call_args.kwargs["recovery_mode"])
        self.assertTrue(cleanup.call_args.kwargs["recovery_mode"])
        self.assertEqual(cleanup.call_args.kwargs["env"]["COMPOSE_FILE"], expected_files)
        self.assertEqual(cleanup.call_args.kwargs["env"]["COMPOSE_ENV_FILES"],
                         up_kwargs["env"]["COMPOSE_ENV_FILES"])
        self.assertEqual(cleanup.call_args.kwargs["env"]["MJOBS_EXTERNAL_SUBMISSIONS_ENABLED"], "0")
        self.assertEqual(cleanup.call_args.kwargs["env"]["MJOBS_OPENAI_API_KEY"], "")
        up_args = next(call for call, _ in docker_calls if "up" in call)
        self.assertIn("--env-file", up_args)
        self.assertEqual(child_env.get("OPENAI_API_KEY"), "")
        watchdog.terminate.assert_called_once()

    def test_recovery_run_rejects_engine_or_image_before_sweep_or_compose(self):
        args = argparse.Namespace(ttl_minutes=60, max_fixtures=1, port=18741,
                                  build=False, recovery=True,
                                  image_ref=self.IMAGE_REF, image_id=self.IMAGE_ID,
                                  docker_context=self.DOCKER_CONTEXT,
                                  command=["--", "python", "-c", "pass"])
        with patch.object(guard, "validate_local_docker_context",
                          side_effect=RuntimeError("remote engine")), \
                patch.object(guard, "sweep") as sweep, \
                patch.object(guard, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "remote engine"):
                guard.run(args)
        sweep.assert_not_called()
        docker.assert_not_called()

        with patch.object(guard, "validate_local_docker_context",
                          return_value=(self.DOCKER_CONTEXT, self.DOCKER_ENDPOINT,
                                        {"DOCKER_HOST": self.DOCKER_ENDPOINT})), \
                patch.object(guard, "validate_local_campaign_image",
                             side_effect=RuntimeError("stale image")), \
                patch.object(guard, "sweep") as sweep, \
                patch.object(guard, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "stale image"):
                guard.run(args)
        sweep.assert_not_called()
        docker.assert_not_called()

    def test_recovery_build_is_rejected_before_preflight_or_writes(self):
        args = argparse.Namespace(build=True, recovery=True, command=["python"])
        with patch.object(guard, "docker") as docker, \
                patch.object(guard, "sweep") as sweep, \
                patch.object(guard, "recovery_environment_file") as dotenv:
            with self.assertRaisesRegex(ValueError, "forbids --build"):
                guard.run(args)
        docker.assert_not_called()
        sweep.assert_not_called()
        dotenv.assert_not_called()

    def test_recovery_cleanup_rejects_default_or_mixed_mode_container_before_docker(self):
        with patch.object(guard, "containers", return_value=[self.labelled()]), \
                patch.object(guard, "docker") as docker:
            with self.assertRaisesRegex(RuntimeError, "unowned container"):
                guard.verify_owned(self.PROJECT, self.TOKEN, recovery_mode=True)
            docker.assert_not_called()

    def test_recovery_cleanup_checks_token_expiry_and_mode_labels_on_resources(self):
        row = self.recovery_labelled()
        volumes, networks = self.recovery_resources()
        network_ids = [f"network-{index}" for index, _ in enumerate(guard.NETWORKS)]
        def fake_docker(*args, **kwargs):
            if args[:2] == ("volume", "ls"):
                return "\n".join(f"{self.PROJECT}_{name}" for name in guard.VOLUMES)
            if args[:2] == ("volume", "inspect"):
                return "\n".join(json.dumps(resource) for resource in volumes)
            if args[:2] == ("network", "ls"):
                return "\n".join(network_ids)
            if args[:2] == ("network", "inspect") and args[2] == "--format":
                return "\n".join(json.dumps(resource) for resource in networks)
            self.fail(f"Unexpected command: {args}")
        with patch.object(guard, "containers", return_value=[row]), \
                patch.object(guard, "docker", side_effect=fake_docker):
            self.assertTrue(guard.verify_owned(self.PROJECT, self.TOKEN, recovery_mode=True))

    def test_recovery_cleanup_accepts_owned_resource_only_partial_start(self):
        volumes, networks = self.recovery_resources()
        with patch.object(guard, "containers", return_value=[]), \
                patch.object(guard, "inspect_resources", side_effect=[volumes, networks]):
            self.assertEqual(guard.verify_owned(self.PROJECT, self.TOKEN, recovery_mode=True),
                             self.LEASE)

    def test_recovery_resource_only_cleanup_rejects_mixed_unowned_or_ambiguous_resources(self):
        good_volumes, good_networks = self.recovery_resources()
        bad_cases = [
            ("volume", {guard.TOKEN_LABEL: "b" * 32}, None, "resource ownership"),
            ("network", {guard.MODE_LABEL: "default"}, None, "resource ownership"),
            ("volume", {guard.EXPIRY_LABEL: "8888888888"}, None, "ambiguous fixture lease"),
            ("network", {guard.PROJECT_LABEL: "mjobs-fixture-ffffffffffff"}, None, "resource ownership"),
            ("volume", {}, f"{self.PROJECT}_foreign", "resource ownership"),
        ]
        for kind, changes, name, message in bad_cases:
            with self.subTest(case=message):
                volumes = [dict(row, Name=name or row["Name"],
                                Labels={**row["Labels"], **changes})
                           for row in good_volumes]
                networks = [dict(row, Name=name or row["Name"],
                                 Labels={**row["Labels"], **changes})
                            for row in good_networks]
                with patch.object(guard, "containers", return_value=[]), \
                        patch.object(guard, "inspect_resources",
                                     side_effect=[volumes if kind == "volume" else good_volumes,
                                                  networks if kind == "network" else good_networks]):
                    with self.assertRaisesRegex(RuntimeError, message):
                        guard.verify_owned(self.PROJECT, self.TOKEN, recovery_mode=True)

    def test_recovery_cleanup_noop_when_failed_start_created_no_resources(self):
        with patch.object(guard, "containers", return_value=[]), \
                patch.object(guard, "inspect_resources", side_effect=[[], []]), \
                patch.object(guard, "docker") as docker:
            self.assertFalse(guard.verify_owned(self.PROJECT, self.TOKEN, recovery_mode=True))
            docker.assert_not_called()

    def test_recovery_resource_only_cleanup_uses_validated_project_and_config_set(self):
        volumes, networks = self.recovery_resources()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        calls = []
        with patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp.name)), \
                patch.object(guard, "containers", return_value=[]), \
                patch.object(guard, "inspect_resources", side_effect=[volumes, networks]), \
                patch.object(guard, "docker",
                             side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or ""):
            self.write_recovery_dotenv(Path(temp.name), expiry=int(self.LEASE))
            guard.cleanup(self.PROJECT, self.TOKEN, recovery_mode=True)
        self.assertEqual(len(calls), 1)
        down, options = calls[0]
        self.assertIn("down", down)
        files = tuple(down[index + 1] for index, item in enumerate(down[:-1]) if item == "--file")
        self.assertEqual(files, guard.compose_files(recovery_mode=True))
        self.assertIn("--env-file", down)
        self.assertEqual(options["env"]["MJOBS_DEV_GUARD_TOKEN"], self.TOKEN)
        self.assertEqual(options["env"]["MJOBS_DEV_GUARD_EXPIRES"], self.LEASE)
        self.assertEqual(options["env"]["MJOBS_IMAGE"], self.IMAGE_ID)
        self.assertEqual(options["env"]["DOCKER_HOST"], self.DOCKER_ENDPOINT)
        self.assertEqual(options["env"]["MJOBS_EXTERNAL_SUBMISSIONS_ENABLED"], "0")

    def test_recovery_inventory_and_expiry_sweep_find_resource_only_project(self):
        volumes, networks = self.recovery_resources()
        with patch.object(guard, "containers", return_value=[]), \
                patch.object(guard, "inspect_resources", side_effect=[volumes, networks]):
            rows = guard.recovery_resource_inventory()
        self.assertEqual(rows, [{"project": self.PROJECT, "containers": 0,
                                 "resources": len(volumes) + len(networks),
                                 "managed": True, "expires": self.LEASE,
                                 "token": self.TOKEN, "mode": guard.RECOVERY_MODE}])
        with patch.object(guard, "inventory", return_value=[]), \
                patch.object(guard, "recovery_resource_inventory", return_value=rows), \
                patch.object(guard, "time") as clock, patch.object(guard, "cleanup") as cleanup:
            clock.time.return_value = int(self.LEASE) + 1
            guard.sweep(apply=True)
        cleanup.assert_called_once_with(self.PROJECT, self.TOKEN, env=None,
                                        recovery_mode=True)

    def test_recovery_sweep_carries_admitted_engine_into_cleanup(self):
        row = {"project": self.PROJECT, "managed": True, "expires": "1",
               "token": self.TOKEN, "containers": 0, "mode": guard.RECOVERY_MODE}
        engine = {"DOCKER_HOST": self.DOCKER_ENDPOINT}
        with patch.object(guard, "inventory", return_value=[row]), \
                patch.object(guard, "recovery_resource_inventory", return_value=[]), \
                patch.object(guard, "time") as clock, patch.object(guard, "cleanup") as cleanup:
            clock.time.return_value = 2
            guard.sweep(apply=True, recovery_only=True, env=engine)
        cleanup.assert_called_once_with(self.PROJECT, self.TOKEN, env=engine,
                                        recovery_mode=True)

    def test_recovery_cleanup_uses_same_root_first_compose_list(self):
        row = self.recovery_labelled()
        calls = []
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp.name)), \
                patch.object(guard, "validate_local_docker_context",
                             return_value=(self.DOCKER_CONTEXT, self.DOCKER_ENDPOINT,
                                           {"DOCKER_HOST": self.DOCKER_ENDPOINT})), \
                patch.object(guard, "validate_local_campaign_image",
                             return_value=self.IMAGE_ID), \
                patch.object(guard, "verify_owned", return_value=self.LEASE) as verify, \
                patch.object(guard, "containers", return_value=[row]), \
                patch.object(guard, "docker", side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or ""):
            self.write_recovery_dotenv(Path(temp.name), expiry=int(self.LEASE))
            guard.cleanup(self.PROJECT, self.TOKEN, recovery_mode=True)
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args, (self.PROJECT, self.TOKEN))
        self.assertEqual(verify.call_args.kwargs["env"]["DOCKER_HOST"], self.DOCKER_ENDPOINT)
        self.assertNotIn("DOCKER_CONTEXT", verify.call_args.kwargs["env"])
        down, options = calls[-1]
        files = tuple(down[index + 1] for index, item in enumerate(down[:-1]) if item == "--file")
        self.assertEqual(files, guard.compose_files(recovery_mode=True))
        self.assertIn("down", down)
        self.assertIn("--volumes", down)
        self.assertEqual(options["env"]["MJOBS_DEV_GUARD_TOKEN"], self.TOKEN)
        self.assertEqual(options["env"]["MJOBS_DEV_GUARD_EXPIRES"], self.LEASE)

    def test_recovery_cleanup_rejects_caller_engine_drift_before_compose(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp.name)), \
                patch.object(guard, "docker") as docker:
            self.write_recovery_dotenv(Path(temp.name), expiry=int(self.LEASE))
            with self.assertRaisesRegex(RuntimeError, "caller engine differs"):
                guard.cleanup(self.PROJECT, self.TOKEN,
                              env={"DOCKER_HOST": "npipe:////./pipe/other"},
                              recovery_mode=True)
        docker.assert_not_called()

    def test_recovery_cleanup_rejects_mismatched_resource_lease_before_down(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp)), \
                patch.object(guard, "verify_owned", return_value="42"), \
                patch.object(guard, "docker") as docker:
            _, dotenv = self.write_recovery_dotenv(Path(temp))
            with self.assertRaisesRegex(RuntimeError, "observed fixture lease differs"):
                guard.cleanup(self.PROJECT, self.TOKEN, recovery_mode=True)
            self.assertTrue(dotenv.is_file())
        docker.assert_not_called()

    def test_inventory_recovers_recovery_mode_for_expiry_sweep(self):
        with patch.object(guard, "containers", return_value=[
                self.recovery_labelled(), self.recovery_labelled(**{guard.SERVICE_LABEL: "worker"})]):
            rows = guard.inventory()
        self.assertEqual(rows, [{"project": self.PROJECT, "containers": 2, "managed": True,
                                 "expires": self.LEASE, "token": self.TOKEN,
                                 "mode": guard.RECOVERY_MODE}])
        with patch.object(guard, "inventory", return_value=rows), \
                patch.object(guard, "recovery_resource_inventory", return_value=[]), \
                patch.object(guard, "time") as clock, patch.object(guard, "cleanup") as cleanup:
            clock.time.return_value = int(self.LEASE) + 1
            guard.sweep(apply=True)
        cleanup.assert_called_once_with(self.PROJECT, self.TOKEN, env=None,
                                        recovery_mode=True)

    def test_default_inventory_keeps_existing_audit_row_shape(self):
        with patch.object(guard, "containers", return_value=[self.labelled()]):
            rows = guard.inventory()
        self.assertEqual(rows, [{"project": self.PROJECT, "containers": 1,
                                 "managed": True, "expires": self.LEASE,
                                 "token": self.TOKEN}])

    def test_recovery_watchdog_spawn_carries_mode_flag(self):
        output = Mock()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(guard, "RECOVERY_ENV_ROOT", Path(temp.name)), \
                patch("builtins.open", return_value=output), \
                patch.object(guard.subprocess, "Popen") as popen:
            self.write_recovery_dotenv(Path(temp.name))
            guard.spawn_watchdog(self.PROJECT, self.TOKEN, 1234567890, recovery_mode=True)
        self.assertEqual(popen.call_args.args[0][-1], "--recovery")
        self.assertEqual(popen.call_args.kwargs["stdin"], guard.subprocess.DEVNULL)
        self.assertNotIn("OPENAI_API_KEY", popen.call_args.kwargs["env"])
        self.assertNotIn("HTTPS_PROXY", popen.call_args.kwargs["env"])
        self.assertIn("COMPOSE_ENV_FILES", popen.call_args.kwargs["env"])
        self.assertEqual(popen.call_args.kwargs["env"]["DOCKER_HOST"], self.DOCKER_ENDPOINT)
        self.assertNotIn("DOCKER_CONTEXT", popen.call_args.kwargs["env"])

    def test_run_parser_defaults_to_existing_mode_and_accepts_explicit_recovery(self):
        with patch.object(guard, "run", return_value=0) as run:
            self.assertEqual(guard.main(["run", "--", "python", "-c", "pass"]), 0)
            self.assertFalse(run.call_args.args[0].recovery)
            self.assertEqual(guard.main(["run", "--recovery", "--", "python", "-c", "pass"]), 0)
            self.assertTrue(run.call_args.args[0].recovery)

    def test_watchdog_recovery_flag_routes_to_matching_cleanup_mode(self):
        with patch.object(guard.time, "sleep"), patch.object(guard, "cleanup") as cleanup:
            guard.watchdog(self.PROJECT, self.TOKEN, 1, recovery_mode=True)
        cleanup.assert_called_once_with(self.PROJECT, self.TOKEN, env=os.environ,
                                        recovery_mode=True, expected_expiry=1)

    def test_watchdog_cli_parses_explicit_recovery_mode(self):
        with patch.object(guard, "watchdog") as watchdog:
            self.assertEqual(guard.main(["watchdog", self.PROJECT, self.TOKEN,
                                         "1234567890", "--recovery"]), 0)
        watchdog.assert_called_once_with(self.PROJECT, self.TOKEN, 1234567890,
                                         recovery_mode=True)

    def test_quota_rejects_existing_live_fixture_including_unmanaged(self):
        args = argparse.Namespace(max_fixtures=1, command=["--", "python", "-c", "pass"])
        with patch.object(guard, "sweep"), \
             patch.object(guard, "containers", return_value=[
                 {guard.PROJECT_LABEL: self.PROJECT}]), \
             patch.object(guard, "spawn_watchdog") as watchdog:
            with self.assertRaisesRegex(RuntimeError, "limit reached"):
                guard.run(args)
            watchdog.assert_not_called()

    def test_normal_run_disposes_project_even_when_test_fails(self):
        args = argparse.Namespace(ttl_minutes=60, max_fixtures=1,
                                  port=18741, build=False,
                                  command=["--", "python", "-m", "pytest"])
        child = Mock()
        child.poll.return_value = None
        subprocess_result = Mock(returncode=7)
        with patch.object(guard, "sweep"), \
             patch.object(guard, "containers", return_value=[]), \
             patch.object(guard, "project_resources", return_value={
                 "containers": [], "volumes": [], "networks": []}), \
             patch.object(guard, "spawn_watchdog", return_value=child), \
             patch.object(guard, "docker", return_value=""), \
             patch.object(guard, "cleanup") as cleanup, \
             patch.object(guard.subprocess, "run", return_value=subprocess_result):
            code = guard.run(args)
        self.assertEqual(code, 7)
        cleanup.assert_called_once()
        child.terminate.assert_called_once()

    def test_lease_config_sets_service_limits_and_no_restarts(self):
        yaml = (Path(__file__).resolve().parents[2] /
                "compose.dev-fixture.yml").read_text(encoding="utf-8")
        self.assertIn('restart: "no"', yaml)
        self.assertIn("com.mjobs.dev.guard:", yaml)
        self.assertIn("com.mjobs.dev.expires:", yaml)
        cpu_values = []
        for line in yaml.splitlines():
            if line.strip().startswith("cpus:"):
                cpu_values.append(float(line.split(":", 1)[1].strip()))
        self.assertEqual(len(cpu_values), len(guard.SERVICES))
        self.assertLessEqual(sum(cpu_values), 4.0)

    def test_recovery_overlay_changes_only_worker_restart_and_labels_every_resource(self):
        yaml = guard.RECOVERY_OVERRIDE.read_text(encoding="utf-8")
        self.assertIn('restart: "on-failure:1"', yaml)
        self.assertEqual(yaml.count("restart:"), 1)
        self.assertIn('MJOBS_EXTERNAL_SUBMISSIONS_ENABLED: "0"', yaml)
        self.assertIn("source-egress:", yaml)
        self.assertEqual(yaml.count("labels: *recovery-labels"),
                         len(guard.SERVICES) + len(guard.VOLUMES) + len(guard.NETWORKS))
        for service in guard.SERVICES - {"worker"}:
            self.assertIn(f"  {service}:\n", yaml)
            self.assertIn("labels: *recovery-labels", yaml)
