from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.agent_dashboard.server import create_app


class AgentDashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init"], cwd=self.root, check=True, capture_output=True)
        (self.root / "prompts").mkdir()
        for name in ("PARALLEL_CODEX_LANE.md", "PARALLEL_ANTIGRAVITY_LANE.md", "PARALLEL_OLLAMA_LANE.md"):
            (self.root / "prompts" / name).write_text("PROMPT\n", encoding="utf-8")
        self.client = TestClient(create_app(self.root))

    def tearDown(self):
        self.temp.cleanup()

    def test_state_is_available_and_loopback_dashboard_has_four_lanes(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body["lanes"]), {"codex", "antigravity", "ollama", "claude"})
        self.assertIn("git", body)
        self.assertIn("coordination", body)

    def test_operation_errors_do_not_disclose_exception_details(self):
        app = self.client.app
        headers = {"X-MJobs-Launch-Token": app.state.mission_control.token}
        cases = (
            ("get", "/api/history", app.state.work_history, "snapshot", 503,
             "Work history is unavailable. Try refreshing again."),
            ("post", "/api/launcher/codex/preview", app.state.mission_control, "preview", 409,
             "Prompt preview failed. Check the selected settings and try again."),
            ("post", "/api/launcher/codex/launch", app.state.mission_control, "launch", 409,
             "Launch failed. Refresh the models and review the prompt before trying again."),
            ("post", "/api/launcher/codex/terminal-window", app.state.supervisor, "open_log_console", 409,
             "Terminal output is unavailable. Check the lane state and try again."),
            ("post", "/api/launcher/codex/stop", app.state.mission_control, "stop", 409,
             "Stop failed. Check the lane state before trying again."),
            ("post", "/api/lanes/codex/start", app.state.supervisor, "start", 409,
             "Start failed. Check the lane configuration before trying again."),
            ("post", "/api/lanes/codex/restart", app.state.supervisor, "restart", 409,
             "Restart failed. Check the lane state and configuration before trying again."),
        )
        # Synthetic diagnostics stand in for filesystem paths, command stderr,
        # credentials or attacker-controlled text embedded in backend errors.
        diagnostic = "PRIVATE_SENTINEL C:/private/profile.env token=synthetic-secret\nTraceback"
        for method, url, target, operation, status, detail in cases:
            for error in (RuntimeError, ValueError, FileNotFoundError, OSError):
                with self.subTest(url=url, error=error.__name__):
                    with patch.object(target, operation, side_effect=error(diagnostic)) as call:
                        response = getattr(self.client, method)(url, headers=headers,
                            **({"json": {"profile": "codex-luna-high", "model": "gpt-6-luna"}}
                               if method == "post" else {}))
                    call.assert_called_once()
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json(), {"detail": detail})

    def test_key_error_handler_does_not_disclose_missing_key(self):
        with patch.object(self.client.app.state.supervisor, "snapshot",
                          side_effect=KeyError("PRIVATE_SENTINEL")):
            response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Requested dashboard resource was not found."})

    def test_error_repair_preserves_authorization_and_unknown_lane_responses(self):
        mission = self.client.app.state.mission_control
        with patch.object(mission, "launch") as launch:
            options = {"profile": "codex-luna-high", "model": "gpt-6-luna"}
            response = self.client.post("/api/launcher/codex/launch", json=options)
            self.assertEqual(response.status_code, 403)
            response = self.client.post("/api/launcher/codex/launch", json=options,
                headers={"X-MJobs-Launch-Token": mission.token, "Origin": "https://evil.example"})
            self.assertEqual(response.status_code, 403)
            launch.assert_not_called()
        response = self.client.get("/api/lanes/unknown/log")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "unknown lane"})

    def test_lane_configuration_can_be_changed_without_starting_process(self):
        response = self.client.patch(
            "/api/lanes/antigravity/config",
            json={"model": "gemini-3.8-flash-medium", "reasoning": "medium", "auto_continue": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "gemini-3.8-flash-medium")
        state = self.client.get("/api/state").json()
        self.assertEqual(state["lanes"]["antigravity"]["runtime"]["status"], "STOPPED")




    def test_terminal_websocket_streams_appended_bytes(self):
        supervisor = self.client.app.state.supervisor
        log = supervisor.log_path("codex")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("old\n", encoding="utf-8")
        supervisor.runtime["lanes"]["codex"].update({
            "run_id": "stream-test",
            "status": "RUNNING",
            "log_start_offset": log.stat().st_size,
            "native_log_path": None,
        })
        with self.client.websocket_connect("/ws/lanes/codex/terminal") as ws:
            first = ws.receive_json()
            self.assertEqual(first["type"], "run")
            with log.open("a", encoding="utf-8") as handle:
                handle.write("LIVE BYTE TEST\n")
                handle.flush()
            deadline = __import__("time").time() + 3
            while True:
                message = ws.receive_json()
                if message.get("type") == "data":
                    self.assertIn("LIVE BYTE TEST", message["data"])
                    break
                if __import__("time").time() > deadline:
                    self.fail("terminal did not stream appended bytes")

    def test_terminal_websocket_emits_run_metadata(self):
        with self.client.websocket_connect("/ws/lanes/codex/terminal") as ws:
            message = ws.receive_json()
            self.assertEqual(message["type"], "run")
            self.assertIn("status", message)

    def test_terminal_websocket_rejects_native_log_outside_log_directory(self):
        supervisor = self.client.app.state.supervisor
        private = self.root / "private.txt"
        private.write_text("PRIVATE SENTINEL", encoding="utf-8")
        supervisor.runtime["lanes"]["antigravity"]["native_log_path"] = str(private)
        supervisor.save()
        supervisor.runtime = supervisor._load_runtime()
        with self.client.websocket_connect("/ws/lanes/antigravity/terminal") as ws:
            self.assertEqual(ws.receive_json()["type"], "run")
            self.assertEqual(ws.receive_json()["type"], "heartbeat")

    def test_terminal_websocket_streams_confined_native_log(self):
        supervisor = self.client.app.state.supervisor
        native = supervisor.native_log_dir / "antigravity-test.log"
        native.write_text("SAFE NATIVE OUTPUT", encoding="utf-8")
        supervisor.runtime["lanes"]["antigravity"]["native_log_path"] = str(native)
        with self.client.websocket_connect("/ws/lanes/antigravity/terminal") as ws:
            self.assertEqual(ws.receive_json()["type"], "run")
            message = ws.receive_json()
            self.assertEqual(message["source"], "native")
            self.assertEqual(message["data"], "SAFE NATIVE OUTPUT")

    def test_ollama_gpu_offload_percent_is_validated_and_persisted(self):
        response = self.client.patch(
            "/api/lanes/ollama/config",
            json={"gpu_offload_percent": 45},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["gpu_offload_percent"], 45)
        bad = self.client.patch(
            "/api/lanes/ollama/config",
            json={"gpu_offload_percent": 120},
        )
        self.assertEqual(bad.status_code, 422)

if __name__ == "__main__":
    unittest.main()
