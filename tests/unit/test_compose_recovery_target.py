"""Unit checks for explicit fixture-only Compose targeting."""
from pathlib import Path
import os
import tempfile
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

    def test_compose_uses_validated_root_and_override_files_for_command_and_labels(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            override = Path(temporary) / "runtime.override.yml"
            override.write_text("services: {}\n", encoding="utf-8")
            compose_files = (str(compose_recovery.COMPOSE_FILE.resolve()), str(override.resolve()))
            with patch.dict(os.environ, {"COMPOSE_FILE": os.pathsep.join(compose_files)}), \
                    patch("cli.compose_recovery._run_bounded", return_value="ok") as runner:
                compose_recovery.compose(VALID_PROJECT, 18701, "ps", "-q", "api")
                self.assertEqual(compose_recovery.expected_config_files_label(compose_files), ",".join(compose_files))

        command, = runner.call_args.args
        first_file = command.index("--file")
        second_file = command.index("--file", first_file + 1)
        self.assertEqual(command[first_file + 1], compose_files[0])
        self.assertEqual(command[second_file + 1], compose_files[1])
        environment = runner.call_args.kwargs["environment"]
        self.assertEqual(environment["COMPOSE_FILE"], os.pathsep.join(compose_files))

    def test_compose_rejects_invalid_file_lists_before_running_docker(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "outside.yml"
            outside_file.write_text("services: {}\n", encoding="utf-8")
            with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                override = Path(temporary) / "runtime.override.yml"
                override.write_text("services: {}\n", encoding="utf-8")
                valid_override = str(override.resolve())
                invalid_lists = (
                    os.pathsep.join((str(compose_recovery.COMPOSE_FILE.resolve()), str(outside_file.resolve()))),
                    os.pathsep.join((valid_override, str(compose_recovery.COMPOSE_FILE.resolve()))),
                    os.pathsep.join((str(compose_recovery.COMPOSE_FILE.resolve()), str(compose_recovery.COMPOSE_FILE.resolve()))),
                    os.pathsep.join((str(compose_recovery.COMPOSE_FILE.resolve()), "")),
                    os.pathsep.join((str(compose_recovery.COMPOSE_FILE.resolve()), temporary)),
                )
                for compose_file_list in invalid_lists:
                    with self.subTest(compose_file_list=compose_file_list), \
                            patch.dict(os.environ, {"COMPOSE_FILE": compose_file_list}), \
                            patch("cli.compose_recovery._run_bounded") as run:
                        with self.assertRaises(ValueError):
                            compose_recovery.compose(VALID_PROJECT, 18701, "ps", "-q", "api")
                        run.assert_not_called()

    def test_compose_rejects_symlink_override_before_running_docker(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            target = Path(temporary) / "real.override.yml"
            link = Path(temporary) / "linked.override.yml"
            target.write_text("services: {}\n", encoding="utf-8")
            compose_file_list = os.pathsep.join((str(compose_recovery.COMPOSE_FILE.resolve()), str(link)))
            is_symlink = Path.is_symlink

            def mark_test_link(path):
                if path == link:
                    return True
                return is_symlink(path)

            with patch.dict(os.environ, {"COMPOSE_FILE": compose_file_list}), \
                    patch.object(Path, "is_symlink", autospec=True, side_effect=mark_test_link), \
                    patch("cli.compose_recovery._run_bounded") as run:
                with self.assertRaisesRegex(ValueError, "symlinks"):
                    compose_recovery.compose(VALID_PROJECT, 18701, "ps", "-q", "api")
                run.assert_not_called()

    def test_compose_rejects_symlink_at_root_compose_path(self):
        root_link = compose_recovery.REPOSITORY_ROOT / "compose.yml"
        is_symlink = Path.is_symlink

        def mark_root_link(path):
            if path == root_link:
                return True
            return is_symlink(path)

        with patch.object(Path, "is_symlink", autospec=True, side_effect=mark_root_link), \
                patch("cli.compose_recovery._run_bounded") as run:
            with self.assertRaisesRegex(ValueError, "symlinks"):
                compose_recovery.compose(VALID_PROJECT, 18701, "ps", "-q", "api")
            run.assert_not_called()

    def test_compose_rejects_junction_override_before_running_docker(self):
        if not hasattr(Path, "is_junction"):
            self.skipTest("Path.is_junction is unavailable")
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            junction_path = Path(temporary) / "junction.override.yml"
            compose_file_list = os.pathsep.join((str(compose_recovery.COMPOSE_FILE.resolve()), str(junction_path)))
            is_junction = Path.is_junction

            def mark_test_junction(path):
                if path == junction_path:
                    return True
                return is_junction(path)

            with patch.dict(os.environ, {"COMPOSE_FILE": compose_file_list}), \
                    patch.object(Path, "is_junction", autospec=True, side_effect=mark_test_junction), \
                    patch("cli.compose_recovery._run_bounded") as run:
                with self.assertRaisesRegex(ValueError, "symlinks"):
                    compose_recovery.compose(VALID_PROJECT, 18701, "ps", "-q", "api")
                run.assert_not_called()

    def test_recovery_restart_targets_include_company_discovery_worker(self):
        self.assertIn("company-discovery-worker", compose_recovery.RECOVERY_RESTART_SERVICES)

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

    def test_validate_implementation_progress_accepts_valid_dict_and_file(self):
        import json
        valid_progress = {
            "current_phase": "M3_VERIFIED",
            "completed_steps": ["Step 1"],
            "files_added": ["file1.txt"],
            "files_modified": ["file2.txt"],
            "tests_completed": ["test 1"],
            "tests_remaining": ["test 2"],
            "migrations_applied": [],
            "live_integrations_tested": [],
            "known_errors": [],
            "known_limitations": [],
            "last_safe_checkpoint": "git:abc1234",
        }
        validated = compose_recovery.validate_implementation_progress(valid_progress)
        self.assertEqual(validated, valid_progress)

        with tempfile.TemporaryDirectory() as temporary:
            progress_file = Path(temporary) / "progress.json"
            progress_file.write_text(json.dumps(valid_progress), encoding="utf-8")
            from_file = compose_recovery.validate_implementation_progress(str(progress_file))
            self.assertEqual(from_file, valid_progress)

    def test_validate_implementation_progress_rejects_invalid_values(self):
        valid_progress = {
            "current_phase": "M3_VERIFIED",
            "completed_steps": ["Step 1"],
            "files_added": ["file1.txt"],
            "files_modified": ["file2.txt"],
            "tests_completed": ["test 1"],
            "tests_remaining": ["test 2"],
            "migrations_applied": [],
            "live_integrations_tested": [],
            "known_errors": [],
            "known_limitations": [],
            "last_safe_checkpoint": "git:abc1234",
        }
        with self.assertRaisesRegex(ValueError, "Implementation progress must be a dictionary"):
            compose_recovery.validate_implementation_progress(12345)

        for missing in ("current_phase", "completed_steps", "last_safe_checkpoint"):
            corrupt = dict(valid_progress)
            del corrupt[missing]
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, f"missing authoritative field: {missing}"):
                    compose_recovery.validate_implementation_progress(corrupt)

        for invalid_list in ("completed_steps", "files_added", "tests_completed"):
            corrupt = dict(valid_progress, **{invalid_list: "not-a-list"})
            with self.subTest(invalid_list=invalid_list):
                with self.assertRaisesRegex(ValueError, f"field {invalid_list} must be a list"):
                    compose_recovery.validate_implementation_progress(corrupt)


if __name__ == "__main__":
    unittest.main()
