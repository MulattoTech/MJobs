"""Compose recovery always targets an explicitly isolated fixture project."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PROJECT = "mjobs-fixture-a1b2c3d4e5f6"
PORT = 23456
BASELINE = {
    "application_id": "application-fixture-1",
    "operation_id": "operation-baseline-1",
    "profile_id": "profile-fixture-1",
    "confirmation": {"receipt_id": "receipt-fixture-1", "submission_count": 1},
}


class _Response:
    def __init__(self, body=None, status_code=200):
        self._body = body or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("unexpected failed fixture API response")

    def json(self):
        return self._body


class ComposeRecoveryTargetTests(unittest.TestCase):
    def test_recovery_uses_one_explicit_fixture_project_and_loopback_port(self):
        from cli import compose_recovery

        compose_calls = []
        http_calls = []
        ready_responses = iter((
            _Response({"checks": {"queue": False}}, status_code=503),
            _Response({"checks": {"queue": True}}),
            _Response({"checks": {"queue": True}}),
        ))

        def request(method, path, **kwargs):
            http_calls.append((method, path, kwargs))
            if method == "POST" and path == "/discovery":
                return _Response({"state": "QUEUED", "operation_id": "operation-recovery-1"})
            if method == "GET" and path == "/operations/operation-recovery-1":
                return _Response({"state": "COMPLETED"})
            if method == "GET" and path.startswith("/profiles/profile-fixture-1/applications/"):
                return _Response({"state": "CONFIRMED", "confirmation": BASELINE["confirmation"]})
            if method == "GET" and path == "/operations/operation-baseline-1":
                return _Response({"state": "COMPLETED"})
            if method == "POST" and path.endswith("/mock-run"):
                return _Response({"operation_id": BASELINE["operation_id"], "state": "COMPLETED"})
            return _Response({})

        client = unittest.mock.MagicMock()
        client.__enter__.return_value = client
        client.request.side_effect = request

        def get(path, **kwargs):
            http_calls.append(("GET", path, kwargs))
            return next(ready_responses)

        client.get.side_effect = get

        def run_bounded(command, *, timeout, action, environment=None):
            compose_calls.append((list(command), dict(environment or {})))
            if command[:2] == ["docker", "compose"]:
                if "ps" in command:
                    return "container-fixture-worker\n"
                if "exec" in command:
                    return json.dumps(BASELINE["confirmation"])
                return ""
            if command[:2] == ["docker", "inspect"]:
                labels = {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": "worker",
                }
                return json.dumps(labels) + "|1\n"
            self.fail(f"unexpected subprocess target: {command!r}")

        root = Path(compose_recovery.__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            baseline_path = Path(temporary) / "baseline.json"
            evidence_path = Path(temporary) / "evidence.json"
            baseline_path.write_text(json.dumps(BASELINE), encoding="utf-8")

            with patch.object(compose_recovery.httpx, "Client", return_value=client) as http_client, \
                    patch.object(compose_recovery, "_run_bounded", side_effect=run_bounded), \
                    patch.object(compose_recovery.subprocess, "Popen", side_effect=AssertionError("subprocess must be stubbed")):
                compose_recovery.run(baseline_path, evidence_path, project_name=PROJECT, port=PORT)

            http_client.assert_called_once_with(
                base_url=f"http://127.0.0.1:{PORT}", timeout=15, trust_env=False,
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertTrue(compose_calls, "recovery should exercise its mocked Compose sequence")
        compose_file = str(root / "compose.yml")
        for command, environment in compose_calls:
            if command[:2] == ["docker", "compose"]:
                self.assertIn("--file", command)
                self.assertEqual(command[command.index("--file") + 1], compose_file)
                self.assertEqual(command[command.index("--profile") + 1], "fixture")
                self.assertEqual(command[command.index("--project-name") + 1], PROJECT)
                self.assertEqual(environment.get("MJOBS_PORT"), str(PORT))
                self.assertEqual(environment.get("COMPOSE_PROFILES"), "fixture")
            else:
                self.assertEqual(command[:2], ["docker", "inspect"])
                self.assertIn("container-fixture-worker", command)

        inspect_calls = [command for command, _ in compose_calls if command[:2] == ["docker", "inspect"]]
        self.assertEqual(len(inspect_calls), 1)
        self.assertTrue(any(command[-3:] == ["ps", "-q", "worker"] for command, _ in compose_calls
                            if command[:2] == ["docker", "compose"]))
        self.assertEqual(evidence["result"], "PASS")

        urls = [call[1] for call in http_calls]
        self.assertIn("/discovery", urls)
        self.assertIn("/operations/operation-recovery-1", urls)

    def test_invalid_or_missing_target_is_rejected_before_compose_or_http(self):
        from cli import compose_recovery

        with tempfile.TemporaryDirectory() as temporary:
            baseline_path = Path(temporary) / "baseline.json"
            evidence_path = Path(temporary) / "evidence.json"
            baseline_path.write_text(json.dumps(BASELINE), encoding="utf-8")

            cases = (
                {"project_name": "mjobs-personal", "port": PORT},
                {"project_name": "mjobs-fixture-not-hex!", "port": PORT},
                {"project_name": PROJECT, "port": 1023},
                {"project_name": PROJECT, "port": 65536},
            )
            for target in cases:
                with self.subTest(target=target):
                    with patch.object(compose_recovery, "_run_bounded") as docker, \
                            patch.object(compose_recovery.httpx, "Client") as http_client:
                        with self.assertRaises(ValueError):
                            compose_recovery.run(baseline_path, evidence_path, **target)
                    docker.assert_not_called()
                    http_client.assert_not_called()

            with patch.object(compose_recovery, "_run_bounded") as docker, \
                    patch.object(compose_recovery.httpx, "Client") as http_client:
                with self.assertRaises(TypeError):
                    compose_recovery.run(baseline_path, evidence_path)
            docker.assert_not_called()
            http_client.assert_not_called()

    def test_cli_requires_and_forwards_explicit_project_and_port(self):
        from cli import compose_recovery

        with patch.object(compose_recovery, "run") as run:
            compose_recovery.main([
                "--baseline", "baseline.json",
                "--evidence", "evidence.json",
                "--project-name", PROJECT,
                "--port", str(PORT),
            ])
        run.assert_called_once_with("baseline.json", "evidence.json", PROJECT, PORT)

        with patch.object(compose_recovery, "run") as run:
            with self.assertRaises(SystemExit):
                compose_recovery.main(["--baseline", "baseline.json", "--evidence", "evidence.json"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
