"""Rendered Compose modes keep the fictional ATS out of personal operation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is unavailable")
class ComposeModeTests(unittest.TestCase):
    def render(self, *arguments, fixture=False):
        environment = os.environ.copy()
        environment["POSTGRES_PASSWORD"] = "synthetic-compose-test-value"
        environment.pop("MJOBS_OPERATION_MODE", None)
        environment.pop("MJOBS_COMPOSE_MOCK", None)
        environment.pop("COMPOSE_PROFILES", None)
        command = ["docker", "compose"]
        if fixture:
            command.extend(["--profile", "fixture"])
            environment["MJOBS_OPERATION_MODE"] = "FIXTURE"
            environment["MJOBS_COMPOSE_MOCK"] = "1"
        command.extend(["config", *arguments])
        return subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                              text=True, timeout=30, check=True).stdout

    def test_default_personal_project_excludes_mock_service(self):
        services = self.render("--services").splitlines()
        configuration = json.loads(self.render("--format", "json"))
        self.assertNotIn("mock", services)
        for name in ("api", "worker", "source-worker", "document-worker"):
            service = configuration["services"][name]
            self.assertEqual(service["environment"]["MJOBS_OPERATION_MODE"], "PERSONAL")
            self.assertEqual(service["environment"]["MJOBS_COMPOSE_MOCK"], "0")
            self.assertNotIn("mock", service.get("depends_on", {}))

    def test_explicit_fixture_project_includes_mock_service(self):
        services = self.render("--services", fixture=True).splitlines()
        configuration = json.loads(self.render("--format", "json", fixture=True))
        self.assertIn("mock", services)
        self.assertEqual(configuration["services"]["api"]["environment"]["MJOBS_OPERATION_MODE"], "FIXTURE")
        self.assertEqual(configuration["services"]["api"]["environment"]["MJOBS_COMPOSE_MOCK"], "1")


if __name__ == "__main__":
    unittest.main()
