"""Unit checks for explicit fixture-only Compose targeting."""
from pathlib import Path
import unittest
from unittest.mock import patch

from cli import compose_recovery


ROOT = Path(__file__).resolve().parents[2]
VALID_PROJECT = "mjobs-fixture-012345abcdef"


class ComposeRecoveryTargetTests(unittest.TestCase):
    def test_compose_uses_explicit_repo_fixture_project_and_port(self):
        with patch("cli.compose_recovery._run_bounded", return_value="worker-id\n") as run:
            result = compose_recovery.compose(VALID_PROJECT, 18701, "ps", "-q", "worker")

        self.assertEqual(result, "worker-id\n")
        command, = run.call_args.args
        self.assertEqual(
            command,
            [
                "docker", "compose",
                "--file", str((ROOT / "compose.yml").resolve()),
                "--project-directory", str(ROOT.resolve()),
                "--profile", "fixture",
                "--project-name", VALID_PROJECT,
                "ps", "-q", "worker",
            ],
        )
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(environment["MJOBS_OPERATION_MODE"], "FIXTURE")
        self.assertEqual(environment["MJOBS_COMPOSE_MOCK"], "1")
        self.assertEqual(environment["COMPOSE_PROFILES"], "fixture")
        self.assertEqual(environment["COMPOSE_PROJECT_NAME"], VALID_PROJECT)
        self.assertEqual(environment["MJOBS_PORT"], "18701")

    def test_cli_requires_explicit_project_name_and_port(self):
        with patch("cli.compose_recovery.run") as run:
            with self.assertRaises(SystemExit) as raised:
                compose_recovery.main(["--baseline", "baseline.json", "--evidence", "evidence.json"])

        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()

    def test_cli_rejects_project_names_outside_fixture_pattern(self):
        unsafe_names = (
            "mjobs-fixture-012345abcdeg",
            "mjobs-fixture-012345ABCDEf",
            "mjobs-personal-012345abcdef",
            "mjobs-fixture-012345abcdef-extra",
        )
        for project_name in unsafe_names:
            with self.subTest(project_name=project_name):
                with patch("cli.compose_recovery.run") as run:
                    with self.assertRaises(SystemExit) as raised:
                        compose_recovery.main([
                            "--baseline", "baseline.json",
                            "--evidence", "evidence.json",
                            "--project-name", project_name,
                            "--port", "18701",
                        ])
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()

    def test_cli_rejects_non_integer_or_out_of_range_ports(self):
        unsafe_ports = ("not-a-port", "1023", "65536")
        for port in unsafe_ports:
            with self.subTest(port=port):
                with patch("cli.compose_recovery.run") as run:
                    with self.assertRaises(SystemExit) as raised:
                        compose_recovery.main([
                            "--baseline", "baseline.json",
                            "--evidence", "evidence.json",
                            "--project-name", VALID_PROJECT,
                            "--port", port,
                        ])
                self.assertEqual(raised.exception.code, 2)
                run.assert_not_called()

    def test_cli_passes_explicit_target_and_inclusive_port_boundaries(self):
        for port in (1024, 65535):
            with self.subTest(port=port):
                with patch("cli.compose_recovery.run") as run:
                    compose_recovery.main([
                        "--baseline", "baseline.json",
                        "--evidence", "evidence.json",
                        "--project-name", VALID_PROJECT,
                        "--port", str(port),
                    ])
                run.assert_called_once_with("baseline.json", "evidence.json", VALID_PROJECT, port)


if __name__ == "__main__":
    unittest.main()
