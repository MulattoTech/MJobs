from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

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
