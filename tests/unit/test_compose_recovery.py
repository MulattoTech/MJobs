import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from cli.compose_recovery import COMPOSE_FILE, REPOSITORY_ROOT, compose


class ComposeRecoveryOutputBoundsTests(unittest.TestCase):
    def test_compose_uses_bounded_runner_and_preserves_small_stdout(self):
        with patch("cli.compose_recovery._run_bounded", return_value="42\n") as run:
            self.assertEqual(compose("mjobs-fixture-012345abcdef", 18877, "version"), "42\n")
        command, = run.call_args.args
        self.assertEqual(command, [
            "docker", "compose", "--file", str(COMPOSE_FILE),
            "--project-directory", str(REPOSITORY_ROOT), "--profile", "fixture",
            "--project-name", "mjobs-fixture-012345abcdef", "version",
        ])
        self.assertEqual(run.call_args.kwargs["timeout"], 90)
        self.assertEqual(run.call_args.kwargs["environment"]["MJOBS_OPERATION_MODE"], "FIXTURE")
        self.assertEqual(run.call_args.kwargs["environment"]["COMPOSE_PROJECT_NAME"], "mjobs-fixture-012345abcdef")
        self.assertEqual(run.call_args.kwargs["environment"]["MJOBS_PORT"], "18877")
        self.assertEqual(run.call_args.kwargs["environment"]["MJOBS_COMPOSE_MOCK"], "1")

    def test_invalid_project_or_port_never_reaches_the_command_runner(self):
        with patch("cli.compose_recovery._run_bounded") as run:
            for project, port in (("production", 18877), ("", 18877),
                                  ("mjobs-fixture-012345abcdef", True),
                                  ("mjobs-fixture-012345abcdef", 80)):
                with self.subTest(project=project, port=port), self.assertRaises(ValueError):
                    compose(project, port, "version")
            run.assert_not_called()

    def test_bounded_runner_terminates_and_reaps_noisy_child(self):
        from cli.compose_recovery import MAX_CAPTURED_OUTPUT_BYTES, _run_bounded

        processes = []
        original_popen = subprocess.Popen

        def track_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        command = [sys.executable, "-c",
                   "import sys,time; sys.stdout.write('x'*3000000); sys.stdout.flush(); time.sleep(30)"]
        started = time.monotonic()
        with patch("cli.compose_recovery.subprocess.Popen", side_effect=track_process):
            with self.assertRaisesRegex(RuntimeError, "captured-output limit"):
                _run_bounded(command, timeout=10, action="Compose test")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll(), "oversized-output child was not reaped")

    def test_bounded_runner_times_out_and_reaps_blocked_child(self):
        from cli.compose_recovery import _run_bounded

        processes = []
        original_popen = subprocess.Popen

        def track_process(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        started = time.monotonic()
        with patch("cli.compose_recovery.subprocess.Popen", side_effect=track_process):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                _run_bounded([sys.executable, "-c", "import time; time.sleep(30)"],
                             timeout=0.1, action="Blocked fixture")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll(), "timed-out child was not reaped")

    def test_bounded_runner_preserves_small_stdout_and_sanitizes_failure(self):
        from cli.compose_recovery import _run_bounded

        result = _run_bounded(
            [sys.executable, "-c", "print('42')"], timeout=2, action="Inspect fixture")
        self.assertEqual(result, "42\n")

        with self.assertRaisesRegex(RuntimeError, "Inspect fixture failed") as raised:
            _run_bounded([sys.executable, "-c",
                          "import sys; print('private-output'); sys.exit(7)"],
                         timeout=2, action="Inspect fixture")
        self.assertNotIn("private-output", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
