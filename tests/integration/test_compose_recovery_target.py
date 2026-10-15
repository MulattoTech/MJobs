"""Compose recovery always targets an explicitly isolated fixture project."""
import json
import os
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
    "request_id": "ui-recovery-request-1",
    "confirmation": {"application_id": "application-fixture-1", "receipt": "MOCK-application-fixture-1",
                      "submission_count": 1},
    "external_submission_enabled": False,
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
        events = []
        ready_responses = iter((
            _Response({"checks": {"queue": False}}, status_code=503),
            _Response({"checks": {"queue": True}}),
            _Response({"checks": {"queue": True}}),
        ))

        def request(method, path, **kwargs):
            http_calls.append((method, path, kwargs))
            events.append(("http", method, path))
            if method == "POST" and path == "/discovery":
                return _Response({"state": "QUEUED", "operation_id": "operation-recovery-1"})
            if method == "GET" and path == "/operations/operation-recovery-1":
                return _Response({"state": "COMPLETED"})
            if method == "GET" and path == "/profiles/profile-fixture-1/applications/application-fixture-1":
                return _Response({"state": "CONFIRMED", "confirmation": BASELINE["confirmation"]})
            if method == "GET" and path == "/operations/operation-baseline-1":
                return _Response({"state": "COMPLETED"})
            if method == "GET" and path.endswith("/mock-runs/" + BASELINE["request_id"]):
                return _Response({"operation_id": BASELINE["operation_id"], "state": "COMPLETED",
                                  "external_submission_enabled": False})
            if method == "POST" and path.endswith("/mock-run"):
                self.assertEqual(kwargs["json"]["request_id"], BASELINE["request_id"])
                return _Response({"operation_id": BASELINE["operation_id"], "state": "COMPLETED",
                                  "replayed": True, "external_submission_enabled": False})
            return _Response({})

        client = unittest.mock.MagicMock()
        client.__enter__.return_value = client
        client.request.side_effect = request

        def get(path, **kwargs):
            http_calls.append(("GET", path, kwargs))
            return next(ready_responses)

        client.get.side_effect = get
        worker_counts = iter((1, 1, 2))
        redis_available = True

        def run_bounded(command, *, timeout, action, environment=None):
            nonlocal redis_available
            compose_calls.append((list(command), dict(environment or {})))
            events.append(("docker", tuple(command)))
            if command[:2] == ["docker", "compose"]:
                if "stop" in command and "redis" in command:
                    redis_available = False
                if "start" in command and "redis" in command:
                    redis_available = True
                if "ps" in command:
                    service = command[-1]
                    return "container-fixture-" + service + "\n"
                if "exec" in command:
                    return json.dumps(BASELINE["confirmation"])
                return ""
            if command[:2] == ["docker", "inspect"]:
                service = command[-1].removeprefix("container-fixture-")
                if ".State.Health" in command[3]:
                    health = "unhealthy" if service == "api" and not redis_available else "healthy"
                    return f"running|{health}\n"
                labels = {
                    "com.docker.compose.project": PROJECT,
                    "com.docker.compose.service": service,
                    "com.docker.compose.project.config_files": compose_recovery.expected_config_files_label(
                        compose_recovery.resolve_compose_files()),
                    "com.docker.compose.project.working_dir": str(compose_recovery.REPOSITORY_ROOT),
                }
                return json.dumps(labels) + "|1|running\n"
            self.fail(f"unexpected subprocess target: {command!r}")

        root = Path(compose_recovery.__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            override_path = Path(temporary) / "runtime.override.yml"
            override_path.write_text("services: {}\n", encoding="utf-8")
            config_paths = (str((root / "compose.yml").resolve()), str(override_path.resolve()))
            baseline_path = Path(temporary) / "baseline.json"
            evidence_path = Path(temporary) / "evidence.json"
            baseline_path.write_text(json.dumps(BASELINE), encoding="utf-8")

            with patch.dict(os.environ, {"COMPOSE_FILE": os.pathsep.join(config_paths)}), \
                    patch.object(compose_recovery.httpx, "Client", return_value=client) as http_client, \
                    patch.object(compose_recovery, "_run_bounded", side_effect=run_bounded), \
                    patch.object(compose_recovery, "worker_restart_count", side_effect=lambda *args, **kwargs: next(worker_counts)) as restart_count, \
                    patch.object(compose_recovery.subprocess, "Popen", side_effect=AssertionError("subprocess must be stubbed")):
                compose_recovery.run(baseline_path, evidence_path, project_name=PROJECT, port=PORT)
                self.assertEqual(restart_count.call_count, 3)

            http_client.assert_called_once_with(
                base_url=f"http://127.0.0.1:{PORT}", timeout=15, trust_env=False,
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertTrue(compose_calls, "recovery should exercise its mocked Compose sequence")
        compose_file = str(root / "compose.yml")
        for command, environment in compose_calls:
            if command[:2] == ["docker", "compose"]:
                file_flags = [index for index, item in enumerate(command) if item == "--file"]
                self.assertEqual([command[index + 1] for index in file_flags], list(config_paths))
                self.assertEqual(command[command.index("--profile") + 1], "fixture")
                self.assertEqual(command[command.index("--project-name") + 1], PROJECT)
                self.assertEqual(environment.get("MJOBS_PORT"), str(PORT))
                self.assertEqual(environment.get("COMPOSE_PROFILES"), "fixture")
                self.assertEqual(environment.get("COMPOSE_FILE"), os.pathsep.join(config_paths))
            else:
                self.assertEqual(command[:2], ["docker", "inspect"])
        self.assertTrue(any(command[:2] == ["docker", "inspect"]
                            and command[-1] == "container-fixture-worker" for command, _ in compose_calls))

        inspect_calls = [command for command, _ in compose_calls
                         if command[:2] == ["docker", "inspect"] and ".Config.Labels" in command[3]]
        self.assertEqual(len(inspect_calls), len(compose_recovery.RECOVERY_MUTATION_SERVICES))
        self.assertTrue(any(command[-4:] == ["ps", "--all", "-q", "worker"] for command, _ in compose_calls
                            if command[:2] == ["docker", "compose"]))
        self.assertTrue(any("stop" in command and "company-discovery-worker" in command
                            for command, _ in compose_calls))
        self.assertTrue(any("start" in command and "company-discovery-worker" in command
                            for command, _ in compose_calls))
        self.assertTrue(any('stop' in command and 'ai-worker' in command for command, _ in compose_calls))
        self.assertTrue(any('start' in command and 'ai-worker' in command for command, _ in compose_calls))
        self.assertEqual(evidence["result"], "PASS")
        self.assertIn("container_health", evidence)
        health = evidence["container_health"]
        self.assertEqual(set(health["before"]), set(compose_recovery.RECOVERY_RESTART_SERVICES))
        self.assertTrue(all(row == {"status": "running", "health": "healthy"}
                            for row in health["before"].values()))
        self.assertEqual(health["redis_outage"]["api"], {"status": "running", "health": "unhealthy"})
        self.assertEqual(health["after"], health["before"])

        urls = [call[1] for call in http_calls]
        self.assertIn("/discovery", urls)
        self.assertIn("/operations/operation-recovery-1", urls)
        recovery_request_path = ("/profiles/profile-fixture-1/applications/application-fixture-1/"
                                 "mock-runs/" + BASELINE["request_id"])
        self.assertIn(recovery_request_path, urls)
        recovery_reads = [index for index, call in enumerate(http_calls)
                          if call[0] == "GET" and call[1] == recovery_request_path]
        self.assertEqual(len(recovery_reads), 2)
        replay_index = next(index for index, call in enumerate(http_calls)
                            if call[0] == "POST" and call[1].endswith("/mock-run"))
        self.assertLess(recovery_reads[0], recovery_reads[1])
        self.assertLess(recovery_reads[1], replay_index)
        restart_event = max(index for index, event in enumerate(events)
                            if event[0] == "docker" and "restart" in event[1])
        replay_event = next(index for index, event in enumerate(events)
                            if event[0] == "http" and event[1] == "POST" and event[2].endswith("/mock-run"))
        post_restart_read_event = max(index for index, event in enumerate(events)
                                      if event[0] == "http" and event[1] == "GET"
                                      and event[2] == recovery_request_path)
        self.assertLess(restart_event, post_restart_read_event)
        self.assertLess(post_restart_read_event, replay_event)
        self.assertEqual(http_calls[replay_index][2]["json"], {
            "submit": True, "request_id": BASELINE["request_id"],
        })
        self.assertTrue(all(call[0] != "POST" or not call[1].endswith("/mock-run")
                            or call[2]["json"].get("request_id") == BASELINE["request_id"]
                            for call in http_calls))
        self.assertTrue(any(command[:2] == ["docker", "compose"]
                            and "ps" in command and "--all" in command for command, _ in compose_calls))
        self.assertEqual(evidence["compose_target"], {
            "project_name": PROJECT,
            "port": PORT,
            "compose_files": list(config_paths),
            "compose_config_files_label": ",".join(config_paths),
            "project_directory": str(Path(compose_recovery.__file__).resolve().parents[1]),
            "profile": "fixture",
            "services": list(compose_recovery.RECOVERY_RESTART_SERVICES),
        })

    def test_unconfirmed_application_never_replays_request(self):
        from cli import compose_recovery

        calls = []
        client = unittest.mock.MagicMock()
        client.__enter__.return_value = client
        client.request.side_effect = lambda method, path, **kwargs: (
            calls.append((method, path, kwargs))
            or _Response({"state": "SUBMISSION_UNCONFIRMED", "confirmation": None})
        )
        with tempfile.TemporaryDirectory() as temporary:
            baseline_path = Path(temporary) / "baseline.json"
            evidence_path = Path(temporary) / "evidence.json"
            baseline_path.write_text(json.dumps(BASELINE), encoding="utf-8")
            with patch.object(compose_recovery, "preflight_recovery_targets", return_value={
                    service: {"status": "running"} for service in compose_recovery.RECOVERY_MUTATION_SERVICES
            }) as preflight, \
                    patch.object(compose_recovery.httpx, "Client", return_value=client), \
                    patch.object(compose_recovery, "compose") as compose:
                with self.assertRaisesRegex(RuntimeError, "same confirmed mock receipt"):
                    compose_recovery.run(baseline_path, evidence_path, PROJECT, PORT)
        preflight.assert_called_once_with(
            PROJECT, PORT, config_files=compose_recovery.resolve_compose_files())
        self.assertFalse(any(method == "POST" and path.endswith("/mock-run")
                             for method, path, _ in calls))
        compose.assert_not_called()

    def test_invalid_ui_baseline_fails_before_runtime_access(self):
        from cli import compose_recovery

        for change in (
            {"request_id": None},
            {"external_submission_enabled": True},
            {"confirmation": {"receipt_id": "receipt-fixture-1", "submission_count": 2}},
            {"profile_id": "fixture/profile?admin=true"},
            {"application_id": "../other-application"},
        ):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                baseline = dict(BASELINE)
                baseline.update(change)
                baseline_path = Path(temporary) / "baseline.json"
                evidence_path = Path(temporary) / "evidence.json"
                baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
                with patch.object(compose_recovery, "_run_bounded") as docker, \
                        patch.object(compose_recovery.httpx, "Client") as client:
                    with self.assertRaises(ValueError):
                        compose_recovery.run(baseline_path, evidence_path, PROJECT, PORT)
                docker.assert_not_called()
                client.assert_not_called()

    def test_container_preflight_failure_has_no_mutation_or_finalizer_start(self):
        from cli import compose_recovery

        label_mismatches = (
            ("com.docker.compose.project", "mjobs-fixture-ffffffffffff"),
            ("com.docker.compose.service", "foreign-service"),
            ("com.docker.compose.project.config_files", "C:/different/compose.yml"),
            ("com.docker.compose.project.working_dir", "C:/different/workdir"),
        )
        for label, foreign_value in label_mismatches:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                commands = []
                service_name = [None]

                def run_bounded(command, *, timeout, action, environment=None):
                    commands.append(list(command))
                    if command[:2] == ["docker", "compose"]:
                        service_name[0] = command[-1]
                        return "foreign-container\n"
                    labels = {
                        "com.docker.compose.project": PROJECT,
                        "com.docker.compose.service": service_name[0],
                        "com.docker.compose.project.config_files": str(compose_recovery.COMPOSE_FILE.resolve()),
                        "com.docker.compose.project.working_dir": str(compose_recovery.REPOSITORY_ROOT.resolve()),
                    }
                    labels[label] = foreign_value
                    return json.dumps(labels) + "|0|running\n"

                baseline_path = Path(temporary) / "baseline.json"
                evidence_path = Path(temporary) / "evidence.json"
                baseline_path.write_text(json.dumps(BASELINE), encoding="utf-8")
                with patch.object(compose_recovery, "_run_bounded", side_effect=run_bounded), \
                        patch.object(compose_recovery.httpx, "Client") as client:
                    with self.assertRaisesRegex(RuntimeError, "unexpected target"):
                        compose_recovery.run(baseline_path, evidence_path, PROJECT, PORT)

                self.assertTrue(commands)
                self.assertTrue(all("stop" not in command and "start" not in command and "restart" not in command
                                    and "up" not in command for command in commands))
                client.assert_not_called()

    def test_preflight_rejects_missing_ambiguous_or_stopped_services(self):
        from cli import compose_recovery

        cases = (("", "missing or ambiguous"), ("container-a\ncontainer-b\n", "missing or ambiguous"),
                 ("container-a\n", "not running"))
        for container_ids, expected_error in cases:
            with self.subTest(container_ids=container_ids, expected_error=expected_error), \
                    tempfile.TemporaryDirectory() as temporary:
                baseline_path = Path(temporary) / "baseline.json"
                evidence_path = Path(temporary) / "evidence.json"
                baseline_path.write_text(json.dumps(BASELINE), encoding="utf-8")
                commands = []
                service_name = [None]

                def run_bounded(command, *, timeout, action, environment=None):
                    commands.append(list(command))
                    if command[:2] == ["docker", "compose"]:
                        service_name[0] = command[-1]
                        return container_ids
                    labels = {
                        "com.docker.compose.project": PROJECT,
                        "com.docker.compose.service": service_name[0],
                        "com.docker.compose.project.config_files": str(compose_recovery.COMPOSE_FILE.resolve()),
                        "com.docker.compose.project.working_dir": str(compose_recovery.REPOSITORY_ROOT.resolve()),
                    }
                    status = "exited" if expected_error == "not running" else "running"
                    return json.dumps(labels) + "|0|" + status + "\n"

                with patch.object(compose_recovery, "_run_bounded", side_effect=run_bounded), \
                        patch.object(compose_recovery.httpx, "Client") as client:
                    with self.assertRaisesRegex(RuntimeError, expected_error):
                        compose_recovery.run(baseline_path, evidence_path, PROJECT, PORT)

                self.assertTrue(commands)
                self.assertTrue(all((command[:2] == ["docker", "compose"] and "ps" in command)
                                    or command[:2] == ["docker", "inspect"] for command in commands))
                client.assert_not_called()

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
