import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import compose_recovery


class ComposeRecoveryTargetGuardrailsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.baseline_path = root / "baseline.json"
        self.baseline_path.write_text(json.dumps({
            "application_id": "fixture-application",
            "operation_id": "fixture-operation",
            "confirmation": {"submission_count": 1},
            "profile_id": "fixture-profile",
        }), encoding="utf-8")
        self.evidence_path = root / "evidence.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def assert_rejected_before_side_effects(self, project_name, port):
        with (
            patch("cli.compose_recovery._run_bounded") as runner,
            patch("cli.compose_recovery.subprocess.Popen") as process,
            patch("cli.compose_recovery.httpx.Client") as http_client,
        ):
            with self.assertRaises(ValueError):
                compose_recovery.run(
                    self.baseline_path, self.evidence_path, project_name, port)

        runner.assert_not_called()
        process.assert_not_called()
        http_client.assert_not_called()

    def test_missing_or_non_fixture_project_is_rejected_before_runtime_access(self):
        for project_name in (None, "", "mjobs", "mjobs-personal", "mjobs-fixture-not-hex123"):
            with self.subTest(project_name=project_name):
                self.assert_rejected_before_side_effects(project_name, 18700)

    def test_missing_or_out_of_range_port_is_rejected_before_runtime_access(self):
        for port in (None, 0, 1023, 65536, "not-a-port"):
            with self.subTest(port=port):
                self.assert_rejected_before_side_effects("mjobs-fixture-012345abcdef", port)

    def test_run_requires_explicit_project_and_port(self):
        with (
            patch("cli.compose_recovery._run_bounded") as runner,
            patch("cli.compose_recovery.subprocess.Popen") as process,
            patch("cli.compose_recovery.httpx.Client") as http_client,
        ):
            with self.assertRaises(TypeError):
                compose_recovery.run(self.baseline_path, self.evidence_path)

        runner.assert_not_called()
        process.assert_not_called()
        http_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
