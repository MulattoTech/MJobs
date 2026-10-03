import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from cli.feedback_gui import create_server


def request(server, method, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}", data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            raw = response.read(); return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class FeedbackGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.server = create_server(0, self.root / "out", self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2); self.temp.cleanup()

    def state(self): return request(self.server, "GET", "/api/state")[1]

    def task(self, index=0):
        tasks = request(self.server, "GET", "/api/tasks")[1]; self.assertTrue(tasks); return tasks[index]

    def post(self, payload, **headers):
        headers.update({"X-MJobs-Token": self.state()["csrf_token"], "Origin": f"http://127.0.0.1:{self.server.server_address[1]}", "Sec-Fetch-Site": "same-origin"})
        return request(self.server, "POST", "/api/reports", payload, headers)

    def draft(self, task=None):
        task = task or self.task(); return {"id": str(uuid.uuid4()), "title": "Draft", "task_id": task["id"], "mode": "PERSONAL", "values": {}}

    def test_draft_survives_restart_and_stale_write_is_rejected(self):
        payload = self.draft(); status, report = self.post(payload); self.assertEqual((status, report["revision"]), (200, 1))
        status, error = self.post({**payload, "expected_revision": 0, "title": "Old"}); self.assertEqual(status, 409); self.assertEqual(error["current"]["title"], "Draft")
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.server = create_server(0, self.root / "out", self.root); self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        reports = self.state()["reports"]; self.assertEqual(len(reports), 1); self.assertEqual(reports[0]["id"], payload["id"])

    def test_invalid_input_origin_csrf_and_host_are_blocked(self):
        payload = {"id": "bad", "title": "", "task_id": "missing", "mode": "NOPE", "values": {}}
        status, body = self.post(payload); self.assertEqual(status, 400); self.assertTrue(body["error"])
        status, _ = request(self.server, "POST", "/api/reports", payload, {"X-MJobs-Token": self.state()["csrf_token"], "Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}); self.assertEqual(status, 403)
        status, _ = request(self.server, "POST", "/api/reports", payload, {"Origin": f"http://127.0.0.1:{self.server.server_address[1]}", "Sec-Fetch-Site": "same-origin"}); self.assertEqual(status, 403)
        status, _ = request(self.server, "GET", "/api/state", headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}); self.assertEqual(status, 403)
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}/api/state", headers={"Host": "127.0.0.1:1"})
        with self.assertRaises(urllib.error.HTTPError) as caught: urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 403)

    def test_invalid_field_and_select_are_rejected(self):
        task = next(t for t in request(self.server, "GET", "/api/tasks")[1] if any(f["kind"] == "select" for f in t["fields"]))
        select = next(f for f in task["fields"] if f["kind"] == "select")
        self.assertEqual(self.post({**self.draft(task), "values": {"unknown": "x"}})[0], 400)
        self.assertEqual(self.post({**self.draft(task), "values": {select["id"]: "not-an-option"}})[0], 400)

    def test_state_exposes_safe_capture_summaries_but_omits_report_dirs(self):
        capture = self.server.feedback_output / "20261003T000000Z"; capture.mkdir(parents=True)
        (capture / "session.json").write_text(json.dumps({"started_at": "now", "event_count": 2, "mode": "PERSONAL", "events": [{"private": "omit"}]}), encoding="utf-8")
        (capture / "events.jsonl").write_text('{"private":"omit"}\n', encoding="utf-8")
        report = self.server.feedback_output / "report-secret"; report.mkdir()
        (report / "session.json").write_text("{}", encoding="utf-8"); (report / "events.jsonl").write_text("", encoding="utf-8")
        state = self.state()
        self.assertIn("branch", state); self.assertIn("commit", state); self.assertIn("captures", state)
        self.assertEqual(state["captures"], [{"id": capture.name, "path": str(capture), "started_at": "now", "event_count": 2, "mode": "PERSONAL"}])
        self.assertNotIn("private", json.dumps(state))

    def test_ready_writes_compatible_artifacts_and_is_idempotent(self):
        task = self.task(); values = {f["id"]: "supplied" for f in task["fields"] if f.get("required")}; payload = {**self.draft(task), "title": "A <job>", "mode": "FIXTURE", "values": values}
        status, report = self.post(payload); self.assertEqual(status, 200)
        status, result = self.post({**payload, "action": "ready", "expected_revision": report["revision"]}); self.assertEqual(status, 200); self.assertEqual(result["status"], "ready")
        folder = Path(result["session_dir"]); self.assertTrue((folder / "feedback.md").is_file()); session = json.loads((folder / "session.json").read_text()); self.assertEqual(session["capture_status"], "NOT_RUN"); self.assertEqual((folder / "events.jsonl").read_text(), "")
        self.assertEqual((self.server.feedback_output / "LATEST.txt").read_text().strip(), folder.name)
        status, again = self.post({**payload, "action": "ready", "expected_revision": result["revision"]}); self.assertEqual(status, 200); self.assertEqual(again["session_dir"], result["session_dir"]); self.assertEqual(len(list(self.server.feedback_output.glob("report-*"))), 1)

    def test_failed_ready_export_keeps_draft_for_retry(self):
        task = self.task(); values = {f["id"]: "supplied" for f in task["fields"] if f.get("required")}; payload = {**self.draft(task), "values": values}; status, saved = self.post(payload); self.assertEqual(status, 200)
        original = self.server.app.write_artifacts; self.server.app.write_artifacts = lambda report, revision: (_ for _ in ()).throw(OSError("synthetic export failure"))
        try: status, _ = self.post({**payload, "action": "ready", "expected_revision": saved["revision"]})
        finally: self.server.app.write_artifacts = original
        self.assertEqual(status, 500); current = self.state()["reports"][0]; self.assertEqual(current["status"], "draft")
        status, ready = self.post({**payload, "action": "ready", "expected_revision": current["revision"]}); self.assertEqual(status, 200); self.assertEqual(ready["status"], "ready")

    def test_ready_retry_repairs_lost_response_and_preserves_revision(self):
        task = self.task(); values = {f["id"]: "supplied" for f in task["fields"] if f.get("required")}; payload = {**self.draft(task), "values": values}
        _, saved = self.post(payload); ready_payload = {**payload, "action": "ready", "expected_revision": saved["revision"]}
        _, first = self.post(ready_payload); (self.server.feedback_output / "LATEST.txt").unlink()
        status, retry = self.post(ready_payload)
        self.assertEqual(status, 200); self.assertEqual(retry["revision"], first["revision"]); self.assertEqual(retry["session_dir"], first["session_dir"])
        self.assertTrue((self.server.feedback_output / "LATEST.txt").is_file())

    def test_database_failure_after_artifacts_does_not_promote_latest(self):
        task = self.task(); values = {f["id"]: "supplied" for f in task["fields"] if f.get("required")}; first_payload = {**self.draft(task), "values": values}
        _, first_saved = self.post(first_payload); _, first = self.post({**first_payload, "action": "ready", "expected_revision": first_saved["revision"]})
        marker = (self.server.feedback_output / "LATEST.txt").read_text()
        second_payload = {**self.draft(task), "values": values}; _, second_saved = self.post(second_payload)
        original = self.server.app.store.put; self.server.app.store.put = lambda report, expected: (_ for _ in ()).throw(__import__("sqlite3").Error("synthetic database failure"))
        try:
            status, _ = self.post({**second_payload, "action": "ready", "expected_revision": second_saved["revision"]})
        finally: self.server.app.store.put = original
        self.assertEqual(status, 500); self.assertEqual((self.server.feedback_output / "LATEST.txt").read_text(), marker)
        self.assertTrue(Path(first["session_dir"]).is_dir())

    def test_editing_ready_report_demotes_it_and_rewrites_handoff(self):
        task = self.task(); values = {f["id"]: "supplied" for f in task["fields"] if f.get("required")}; payload = {**self.draft(task), "values": values}
        _, saved = self.post(payload); _, ready = self.post({**payload, "action": "ready", "expected_revision": saved["revision"]})
        edited = {**payload, "title": "Changed title", "expected_revision": ready["revision"]}; status, draft = self.post(edited)
        self.assertEqual(status, 200); self.assertEqual(draft["status"], "draft"); self.assertIsNone(draft["session_dir"])
        status, rewritten = self.post({**edited, "action": "ready", "expected_revision": draft["revision"]})
        self.assertEqual(status, 200); self.assertEqual(rewritten["status"], "ready")
        self.assertIn("Changed title", (Path(rewritten["session_dir"]) / "feedback.md").read_text(encoding="utf-8"))

    def test_static_routes_have_restrictive_csp(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}/")
        with urllib.request.urlopen(req) as response: self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])


if __name__ == "__main__": unittest.main()
