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
        environment["MJOBS_CREDENTIAL_ENCRYPTION_KEY"] = "synthetic-base64-test-key"
        environment.pop("MJOBS_OPERATION_MODE", None)
        environment.pop("MJOBS_COMPOSE_MOCK", None)
        environment.pop("MJOBS_BROWSER_DEBUG_CAPTURE", None)
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
        api_environment = configuration["services"]["api"]["environment"]
        self.assertEqual(api_environment["MJOBS_CREDENTIAL_ENCRYPTION_KEY"], "synthetic-base64-test-key")
        self.assertNotIn("MJOBS_CREDENTIAL_ENCRYPTION_KEY", configuration["services"]["worker"]["environment"])
        self.assertIn("source-egress", configuration["services"]["api"]["networks"])
        for name in ("api", "scheduler", "worker", "source-worker", "document-worker"):
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

    def test_durable_dependencies_restart_with_runtime(self):
        configuration = json.loads(self.render("--format", "json"))
        services = configuration["services"]
        for name in ("postgres", "redis", "api", "scheduler", "worker", "source-worker", "document-worker"):
            with self.subTest(service=name):
                self.assertEqual(services[name]["restart"], "unless-stopped")
        self.assertNotIn("restart", services["migrate"])

    def test_browser_document_and_discovery_have_distinct_consumers(self):
        configuration = json.loads(self.render("--format", "json"))
        services = configuration["services"]
        queues = {name: services[name]["environment"]["MJOBS_WORKER_QUEUE"]
                  for name in ("worker", "source-worker", "document-worker")}
        self.assertEqual(queues, {"worker": "mjobs-application", "source-worker": "mjobs-discovery",
                                  "document-worker": "mjobs-document"})
        self.assertEqual(len(set(queues.values())), len(queues))
        self.assertEqual(services["scheduler"].get("networks"), {"private": None})
        self.assertIn("source-egress", services["source-worker"].get("networks", {}))

    def test_runtime_workers_receive_five_independent_concurrency_limits(self):
        configuration = json.loads(self.render("--format", "json"))
        names = {
            "MJOBS_SOURCE_COLLECTION_CONCURRENCY": "2",
            "MJOBS_COMPANY_DISCOVERY_CONCURRENCY": "1",
            "MJOBS_AI_CONCURRENCY": "1",
            "MJOBS_DOCUMENT_GENERATION_CONCURRENCY": "1",
            "MJOBS_APPLICATION_AUTOMATION_CONCURRENCY": "1",
        }
        for service_name in ("worker", "source-worker", "document-worker"):
            environment = configuration["services"][service_name]["environment"]
            with self.subTest(service=service_name):
                self.assertEqual({name: environment[name] for name in names}, names)

    def test_browser_debug_capture_is_disabled_by_default(self):
        configuration = json.loads(self.render("--format", "json"))
        for service_name in ("worker", "source-worker", "document-worker"):
            with self.subTest(service=service_name):
                self.assertEqual(
                    configuration["services"][service_name]["environment"]["MJOBS_BROWSER_DEBUG_CAPTURE"],
                    "0",
                )

    def test_browser_debug_capture_can_be_opted_in_for_runtime_workers(self):
        environment = os.environ.copy()
        environment["MJOBS_BROWSER_DEBUG_CAPTURE"] = "1"
        environment["POSTGRES_PASSWORD"] = "synthetic-compose-test-value"
        environment["MJOBS_CREDENTIAL_ENCRYPTION_KEY"] = "synthetic-base64-test-key"
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"], cwd=ROOT,
            env=environment, capture_output=True, text=True, timeout=30, check=True,
        )
        configuration = json.loads(result.stdout)
        for service_name in ("worker", "source-worker", "document-worker"):
            with self.subTest(service=service_name):
                self.assertEqual(
                    configuration["services"][service_name]["environment"]["MJOBS_BROWSER_DEBUG_CAPTURE"],
                    "1",
                )

    def test_every_service_uses_bounded_rotating_container_logs(self):
        configuration = json.loads(self.render("--format", "json", fixture=True))
        services = configuration["services"]

        self.assertEqual(set(services), {
            "postgres", "redis", "migrate", "api", "mock", "worker",
            "scheduler", "source-worker", "document-worker",
        })
        for name, service in services.items():
            with self.subTest(service=name):
                logging = service["logging"]
                self.assertEqual(logging["driver"], "json-file")
                self.assertEqual(logging["options"]["max-size"], "10m")
                self.assertEqual(logging["options"]["max-file"], "5")


if __name__ == "__main__":
    unittest.main()
