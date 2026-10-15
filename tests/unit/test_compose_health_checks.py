"""Only bounded, status-only health evidence can certify fixture recovery."""
import unittest
from unittest.mock import patch

from cli import compose_recovery as recovery


class ComposeHealthChecksTests(unittest.TestCase):
    def test_inspection_is_bounded_and_returns_only_health_state(self):
        with patch.object(recovery, "_run_bounded", return_value="running|unhealthy\n") as run:
            self.assertEqual(recovery.container_health("owned-container"), {
                "status": "running", "health": "unhealthy"})
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "inspect", "--format"])
        self.assertEqual(command[-1], "owned-container")
        self.assertNotIn(".Config.Env", command[3])
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_missing_or_invalid_health_cannot_be_accepted(self):
        for value in ("running|missing", "running|", "PRIVATE_SENTINEL", "running|healthy|extra",
                      "unknown|healthy", "running|PRIVATE_SENTINEL"):
            with self.subTest(value=value), patch.object(recovery, "_run_bounded", return_value=value):
                with self.assertRaisesRegex(RuntimeError, "health") as caught:
                    recovery.container_health("owned-container")
                self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))

    def test_every_long_running_container_must_be_running_and_healthy(self):
        targets = {name: {"container_id": name} for name in recovery.RECOVERY_RESTART_SERVICES}
        healthy = {"status": "running", "health": "healthy"}
        for failure in ({"status": "running", "health": "unhealthy"},
                        {"status": "running", "health": "starting"},
                        {"status": "restarting", "health": "healthy"}):
            with self.subTest(failure=failure), patch.object(
                    recovery, "container_health", side_effect=lambda name: failure if name == "scheduler" else healthy):
                self.assertFalse(recovery.healthy_containers(targets))
        with patch.object(recovery, "container_health", return_value=healthy):
            self.assertEqual(set(recovery.healthy_containers(targets)), set(targets))

    def test_incomplete_targets_refuse_before_docker_access(self):
        with patch.object(recovery, "_run_bounded") as run:
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                recovery.healthy_containers({"api": {"container_id": "owned-container"}})
        run.assert_not_called()
