"""No-Docker safety tests for the opt-in disposable fixture lifecycle."""
import argparse
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from tools import dev_fixture_guard as guard


class FixtureGuardTests(TestCase):
    PROJECT = "mjobs-fixture-012345abcdef"
    TOKEN = "a" * 32
    LEASE = "9999999999"

    def labelled(self, **overrides):
        row = {
            guard.PROJECT_LABEL: self.PROJECT,
            guard.TOKEN_LABEL: self.TOKEN,
            guard.EXPIRY_LABEL: self.LEASE,
            guard.SERVICE_LABEL: "api",
        }
        row.update(overrides)
        return row

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
        self.assertNotEqual(env["POSTGRES_PASSWORD"], "production-pass")
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], self.PROJECT)
        self.assertEqual(env["MJOBS_OPERATION_MODE"], "FIXTURE")
        self.assertEqual(env["MJOBS_COMPOSE_MOCK"], "1")

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
