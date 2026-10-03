"""Small, local-only GUI server for durable live-feedback reports."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "outputs" / "live-feedback"
MAX_BODY = 1_000_000
MAX_TITLE = 240
MAX_VALUE = 20_000


def now():
    return datetime.now(timezone.utc).isoformat()


def _atomic(path: Path, data: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class FeedbackStore:
    def __init__(self, output_root: Path, workspace: Path):
        self.output_root = output_root.resolve()
        self.workspace = workspace.resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.output_root / "feedback.sqlite3"
        self.lock = threading.RLock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, task_id TEXT NOT NULL,
                mode TEXT NOT NULL, values_json TEXT NOT NULL, revision INTEGER NOT NULL,
                status TEXT NOT NULL, updated_at TEXT NOT NULL, session_dir TEXT
            )""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self):
        with self.lock, self._db() as db:
            return [self._row(row) for row in db.execute("SELECT * FROM reports ORDER BY updated_at DESC")]

    @staticmethod
    def _row(row):
        item = dict(row)
        item["values"] = json.loads(item.pop("values_json"))
        return item

    def get(self, report_id):
        with self.lock, self._db() as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            return self._row(row) if row else None

    def put(self, report, expected):
        with self.lock, self._db() as db:
            old = db.execute("SELECT * FROM reports WHERE id=?", (report["id"],)).fetchone()
            if old and expected != old["revision"]:
                raise Conflict(self._row(old))
            if not old and expected not in (None, 0):
                raise Conflict(None)
            revision = (old["revision"] + 1) if old else 1
            report["revision"] = revision
            report["updated_at"] = now()
            db.execute("""INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title, task_id=excluded.task_id,
                mode=excluded.mode, values_json=excluded.values_json, revision=excluded.revision,
                status=excluded.status, updated_at=excluded.updated_at, session_dir=excluded.session_dir""",
                (report["id"], report["title"], report["task_id"], report["mode"],
                 json.dumps(report["values"], ensure_ascii=False), revision, report["status"],
                 report["updated_at"], report.get("session_dir")))
            return report


class Conflict(Exception):
    def __init__(self, current):
        self.current = current


class FeedbackHandler(BaseHTTPRequestHandler):
    server_version = "MJobsFeedback/1"

    def log_message(self, *_args):
        return

    @property
    def app(self):
        return self.server.app

    def _headers(self, content_type="application/json; charset=utf-8"):
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status); self._headers(); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def _bad(self, status, message, current=None):
        body = {"error": message}
        if current is not None: body["current"] = current
        self._json(status, body)

    def _host_ok(self):
        return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_address[1]}"

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin:
            expected = f"http://127.0.0.1:{self.server.server_address[1]}"
            if origin != expected: return False
        fetch = self.headers.get("Sec-Fetch-Site")
        return not fetch or fetch in {"same-origin", "same-site", "none"}

    def do_GET(self):
        if not self._host_ok() or not self._origin_ok(): return self._bad(403, "Request origin not allowed")
        path = urlsplit(self.path).path
        if path == "/api/tasks": return self._json(200, self.app.tasks)
        if path == "/api/state":
            return self._json(200, {"service": "mjobs-feedback-gui", "workspace": str(self.app.store.workspace), "output_root": str(self.app.store.output_root), "branch": self.app.branch, "commit": self.app.commit, "captures": self.app.captures(), "reports": self.app.store.rows(), "csrf_token": self.app.csrf})
        if path in {"/", "/index.html", "/app.js", "/style.css"}:
            name = "index.html" if path == "/" else path[1:]
            source = self.app.ui / name
            if not source.is_file(): return self._bad(404, "UI asset not found")
            data = source.read_bytes(); types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
            self.send_response(200); self._headers(types.get(source.suffix, "text/plain")); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        self._bad(404, "Route not found")

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok(): return self._bad(403, "Request origin not allowed")
        if self.headers.get("X-MJobs-Token") != self.app.csrf: return self._bad(403, "CSRF token required")
        if not self.headers.get("Content-Type", "").split(";", 1)[0].lower() == "application/json": return self._bad(415, "JSON content type required")
        if urlsplit(self.path).path != "/api/reports": return self._bad(404, "Route not found")
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if size < 0 or size > MAX_BODY: raise ValueError("body too large")
            payload = json.loads(self.rfile.read(size))
            with self.app.store.lock:
                report = self.app.validate(payload)
                existing = self.app.store.get(report["id"])
                expected = payload.get("expected_revision", payload.get("revision"))
                same_ready = existing and payload.get("action", "save") == "ready" and existing["status"] == "ready" and all(report[key] == existing[key] for key in ("title", "task_id", "mode", "values"))
                if same_ready:
                    self.app.publish_latest(existing)
                    return self._json(200, existing)
                if existing and expected is None: raise Conflict(existing)
                if existing and expected != existing["revision"]: raise Conflict(existing)
                if not existing and expected not in (None, 0): raise Conflict(None)
                if payload.get("action", "save") == "ready":
                    self.app.validate_ready(report)
                    report["status"] = "ready"
                    report = self.app.write_artifacts(report, (existing["revision"] + 1) if existing else 1)
                elif payload.get("action", "save") != "save": raise ValueError("invalid action")
                elif existing:
                    report["status"] = "draft"
                    report["session_dir"] = None
                result = self.app.store.put(report, expected)
                if result["status"] == "ready": self.app.publish_latest(result)
            self._json(200, result)
        except Conflict as exc:
            self._bad(409, "stale revision", exc.current)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._bad(400, str(exc)[:200])
        except OSError:
            self._bad(500, "artifact write failed")
        except sqlite3.Error:
            self._bad(500, "draft storage failed")


class FeedbackApp:
    def __init__(self, output_root, workspace, ui):
        self.store = FeedbackStore(Path(output_root), Path(workspace))
        self.ui = Path(ui)
        self.csrf = secrets.token_urlsafe(32)
        self.branch, self.commit = self._git_identity()
        task_file = ROOT / "cli" / "feedback_tasks.json"
        try: self.tasks = json.loads(task_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc: raise RuntimeError("feedback task catalogue is unavailable") from exc
        if not isinstance(self.tasks, list) or not self.tasks: raise RuntimeError("feedback task catalogue is invalid")
        self.by_id = {str(t.get("id")): t for t in self.tasks if isinstance(t, dict) and t.get("id")}

    def _git_identity(self):
        def run(*args):
            try:
                result = subprocess.run(["git", "-C", str(self.store.workspace), *args], capture_output=True, text=True, timeout=2, check=True)
                value = result.stdout.strip()
                return value or "unavailable"
            except (OSError, subprocess.SubprocessError):
                return "unavailable"
        return run("branch", "--show-current"), run("rev-parse", "--short", "HEAD")

    def captures(self):
        result = []
        try:
            children = sorted((p for p in self.store.output_root.iterdir() if p.is_dir() and not p.name.startswith("report-")), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return result
        for folder in children:
            if len(result) >= 50 or not (folder / "session.json").is_file() or not (folder / "events.jsonl").is_file():
                continue
            item = {"id": folder.name, "path": str(folder)}
            try:
                data = json.loads((folder / "session.json").read_text(encoding="utf-8"))
                for key in ("started_at", "event_count", "mode"):
                    if key in data and isinstance(data[key], (str, int, float)):
                        item[key] = data[key]
            except (OSError, ValueError, TypeError):
                pass
            result.append(item)
        return result

    def validate(self, payload):
        if not isinstance(payload, dict): raise ValueError("JSON object required")
        rid = payload.get("id")
        try: rid = str(uuid.UUID(str(rid)))
        except (ValueError, TypeError, AttributeError): raise ValueError("id must be UUID")
        title = payload.get("title", "")
        if not isinstance(title, str) or len(title) > MAX_TITLE: raise ValueError("invalid title")
        task_id = payload.get("task_id")
        task = self.by_id.get(str(task_id))
        if not task: raise ValueError("unknown task")
        mode = payload.get("mode", "PERSONAL")
        if mode not in {"PERSONAL", "FIXTURE"}: raise ValueError("invalid mode")
        values = payload.get("values", {})
        if not isinstance(values, dict): raise ValueError("values must be object")
        definitions = {str(f.get("id")): f for f in task.get("fields", []) if isinstance(f, dict) and f.get("id")}
        for key, value in values.items():
            if key not in definitions: raise ValueError(f"unknown field: {key}")
            field = definitions[key]
            if not isinstance(value, str) or len(value) > int(field.get("max_length", MAX_VALUE)): raise ValueError(f"invalid field: {key}")
            if field.get("kind") == "select" and value and value not in field.get("options", []): raise ValueError(f"invalid option: {key}")
        return {"id": rid, "title": title, "task_id": str(task_id), "mode": mode, "values": values, "status": "draft", "session_dir": None}

    def validate_ready(self, report):
        if not report["title"].strip(): raise ValueError("title is required")
        task = self.by_id[report["task_id"]]
        for field in task.get("fields", []):
            if field.get("required") and not str(report["values"].get(field.get("id"), "")).strip(): raise ValueError(f"required field: {field.get('id')}")

    def write_artifacts(self, report, revision):
        folder = self.store.output_root / f"report-{report['id']}-r{revision}"
        folder.mkdir(parents=True, exist_ok=True)
        safe = lambda value: str(value).replace("\\", "\\\\").replace("`", "\\`").replace("\r", "").replace("\n", "\n> ")
        lines = ["# MJobs Live Feedback", "", f"- Title: {safe(report['title'])}", f"- Task: {safe(report['task_id'])}", f"- Mode: {report['mode']}", "", "## Supplied fields", ""]
        for key, value in report["values"].items(): lines.append(f"- {safe(key)}: {safe(value)}")
        _atomic(folder / "feedback.md", "\n".join(lines) + "\n")
        _atomic(folder / "events.jsonl", "")
        _atomic(folder / "session.json", json.dumps({"schema_version": 1, "started_at": now(), "ended_at": now(), "base_url": "", "final_url": "", "page_title": "MJobs Feedback GUI", "visuals": False, "diagnostic_text": False, "event_count": 0, "events": [], "capture_status": "NOT_RUN"}, indent=2) + "\n")
        report["session_dir"] = str(folder)
        return report

    def publish_latest(self, report):
        folder = Path(report["session_dir"]).resolve()
        if not folder.is_relative_to(self.store.output_root) or not folder.is_dir():
            raise OSError("ready report artifacts are unavailable")
        _atomic(self.store.output_root / "LATEST.txt", folder.name + "\n")


def create_server(port=18702, output_root=DEFAULT_OUTPUT, workspace=ROOT, ui=None):
    app = FeedbackApp(output_root, workspace, Path(ui) if ui else ROOT / "cli" / "feedback_ui")
    server = ThreadingHTTPServer(("127.0.0.1", port), FeedbackHandler)
    server.app = app
    server.feedback_output = app.store.output_root
    return server


def main():
    parser = argparse.ArgumentParser(description="MJobs local feedback GUI")
    parser.add_argument("--port", type=int, default=18702); parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT); parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(); server = create_server(args.port, args.output_root)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    if not args.no_open: webbrowser.open(url)
    print(url, flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
