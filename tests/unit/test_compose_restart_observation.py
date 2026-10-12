"""Restart observation must tolerate worker downtime without relaxing preflight."""
import json
import unittest
from unittest.mock import patch

from cli import compose_recovery as recovery


class ComposeRestartObservationTests(unittest.TestCase):
    def inspection(self, status, **overrides):
        labels = {
            "com.docker.compose.project": "mjobs-fixture-012345abcdef",
            "com.docker.compose.service": "worker",
            "com.docker.compose.project.config_files": str(recovery.COMPOSE_FILE.resolve()),
            "com.docker.compose.project.working_dir": str(recovery.REPOSITORY_ROOT.resolve()),
        }
        labels.update(overrides)
        return json.dumps(labels) + "|2|" + status

    def test_restart_count_is_read_while_validated_worker_is_restarting(self):
        for status in ("running", "restarting", "exited"):
            with self.subTest(status=status), patch.object(recovery, "compose", return_value="worker-id"), patch.object(
                recovery, "_run_bounded", return_value=self.inspection(status)
            ):
                self.assertEqual(recovery.worker_restart_count("mjobs-fixture-012345abcdef", 18701), 2)

    def test_mutation_preflight_still_rejects_nonrunning_worker(self):
        for status in ("restarting", "exited"):
            with self.subTest(status=status), patch.object(recovery, "_run_bounded", return_value=self.inspection(status)):
                with self.assertRaisesRegex(RuntimeError, "not running"):
                    recovery.inspect_container("mjobs-fixture-012345abcdef", "worker", "worker-id")

    def test_observation_rejects_foreign_worker_even_during_restart(self):
        for key in ("com.docker.compose.project", "com.docker.compose.service",
                    "com.docker.compose.project.config_files", "com.docker.compose.project.working_dir"):
            with self.subTest(key=key), patch.object(recovery, "compose", return_value="worker-id"), patch.object(
                recovery, "_run_bounded", return_value=self.inspection("restarting", **{key: "foreign"})
            ):
                with self.assertRaisesRegex(RuntimeError, "unexpected target"):
                    recovery.worker_restart_count("mjobs-fixture-012345abcdef", 18701)

    def test_observation_rejects_dead_or_unknown_worker_status(self):
        for status in ("dead", "paused", "unknown"):
            with self.subTest(status=status), patch.object(recovery, "compose", return_value="worker-id"), patch.object(
                recovery, "_run_bounded", return_value=self.inspection(status)
            ):
                with self.assertRaises(RuntimeError):
                    recovery.worker_restart_count("mjobs-fixture-012345abcdef", 18701)
