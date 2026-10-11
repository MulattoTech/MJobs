"""Durable queued resume extraction preserves pending-only review semantics."""
from datetime import datetime, timedelta, timezone
import hashlib
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
import uvicorn
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.documents.uploads import save_resume
from tests.integration.test_spine import PROFILE
from workers.operations import claim, recover_expired
from workers.tasks import execute_operation
from fixtures.browser_ports import bound_browser_socket


class DurableResumeExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-durable-resume-extraction-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database_path = self.root / "test.db"
        self.database_url = f"sqlite:///{self.database_path.as_posix()}"
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.profile_id = "durable-extract-" + uuid4().hex[:12]
        self.other_profile_id = self.profile_id + "-other"
        self.request_id = "intent-" + uuid4().hex
        self.store.save_profile(dict(PROFILE, id=self.profile_id, email=f"{self.profile_id}@example.test"))
        self.store.save_profile(dict(PROFILE, id=self.other_profile_id, email=f"{self.other_profile_id}@example.test"))
        self.resume_bytes = (
            b"Synthetic Applicant\nSkills\nPython, PostgreSQL\n"
            b"Experience\nBuilt durable local systems.\n"
        )
        self.resume = save_resume(
            self.store, self.root, self.profile_id, "synthetic.txt", "text/plain", self.resume_bytes)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="PERSONAL"))
        self.addCleanup(self.client.close)

    def queue(self, profile_id=None, resume_id=None, request_id=None):
        profile_id = profile_id or self.profile_id
        resume_id = resume_id or self.resume["id"]
        return self.client.post(
            f"/profiles/{profile_id}/resumes/{resume_id}/extract/queued",
            json={"request_id": request_id or self.request_id})

    def operation(self, operation_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id)).mappings().one())

    def test_queue_replay_uses_only_immutable_resume_identity_and_exposes_no_suggestions(self):
        first = self.queue()
        self.assertEqual(first.status_code, 202, first.text)
        first_payload = first.json()
        self.assertEqual(first_payload["state"], "QUEUED")
        second = self.queue()
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["operation_id"], first_payload["operation_id"])
        operation = self.operation(first_payload["operation_id"])
        self.assertEqual(operation["kind"], "RESUME_EXTRACT_QUEUED")
        self.assertEqual(operation["entity_id"], self.resume["id"])
        self.assertEqual(set(operation["params"]), {
            "profile_id", "resume_id", "resume_sha256", "request_id", "extractor_version",
        })
        self.assertEqual(operation["params"]["request_id"], self.request_id)
        self.assertEqual(operation["params"]["resume_sha256"], hashlib.sha256(self.resume_bytes).hexdigest())
        self.assertNotIn("Synthetic Applicant", repr(operation["params"]))
        all_operations = self.client.get("/operations").json()["operations"]
        self.assertNotIn("Built durable local systems.", repr(all_operations))
        progress = self.client.get(f"/profiles/{self.profile_id}/operations/progress").json()["operations"]
        self.assertIn(first_payload["operation_id"], {row["operation_id"] for row in progress})

    def test_lost_http_ack_after_commit_replays_the_existing_operation(self):
        from apps.api import main as api_main
        real_queue = api_main.queue_durable_extraction
        app = create_app(self.store, "http://127.0.0.1:1", self.root, operation_mode="PERSONAL")
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)

        def commit_then_lose_ack(*args, **kwargs):
            real_queue(*args, **kwargs)
            raise RuntimeError("simulated lost response after durable admission")

        with patch.object(api_main, "queue_durable_extraction", side_effect=commit_then_lose_ack):
            lost = client.post(
                f"/profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/queued",
                json={"request_id":self.request_id})
        self.assertEqual(lost.status_code, 500)
        self.assertEqual(self.store.count("worker_operations"), 1)
        replay = client.post(
            f"/profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/queued",
            json={"request_id":self.request_id})
        self.assertEqual(replay.status_code, 202, replay.text)
        operation_id = replay.json()["operation_id"]
        self.assertEqual(self.operation(operation_id)["state"], "QUEUED")
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_real_http_disconnect_after_durable_admission_leaves_operation_queued(self):
        from apps.api import main as api_main
        real_queue = api_main.queue_durable_extraction
        committed, release = threading.Event(), threading.Event()
        app = create_app(self.store, "http://127.0.0.1:1", self.root, operation_mode="PERSONAL")
        listener = bound_browser_socket()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        server_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def commit_then_wait(*args, **kwargs):
            queued = real_queue(*args, **kwargs)
            committed.set()
            if not release.wait(5):
                raise TimeoutError("test disconnect response gate was not released")
            return queued

        server_thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not server_thread.is_alive() or time.monotonic() > deadline:
                    self.fail("isolated API did not start")
                time.sleep(0.02)
            with patch.object(api_main, "queue_durable_extraction", side_effect=commit_then_wait):
                client_socket = socket.create_connection(("127.0.0.1", port), timeout=5)
                body = ('{"request_id":"' + self.request_id + '"}').encode("ascii")
                request = (
                    f"POST /profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/queued HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode("ascii") + body
                client_socket.sendall(request)
                self.assertTrue(committed.wait(5), "request was not durably admitted")
                client_socket.close()
                release.set()
            self.assertEqual(self.store.count("worker_operations"), 1)
            with self.store.engine.connect() as connection:
                operation_id = connection.execute(select(m.operations.c.id).where(
                    m.operations.c.kind == "RESUME_EXTRACT_QUEUED")).scalar_one()
            operation = self.operation(operation_id)
            self.assertEqual(operation["state"], "QUEUED")
            replay = self.queue(request_id=self.request_id)
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertEqual(replay.json()["operation_id"], operation["id"])
        finally:
            release.set()
            server.should_exit = True
            server_thread.join(timeout=10)
            listener.close()
            self.assertFalse(server_thread.is_alive())
    def test_completed_result_is_private_pending_only_and_profile_is_unchanged(self):
        queued = self.queue()
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        before_profile = self.store.profile(self.profile_id)
        worker_env = {
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "PYTHONUTF8": "1",
        }
        from services.career_profile import durable_extraction
        real_extract = durable_extraction.extract_resume
        calls = []

        def count_extract(*args, **kwargs):
            calls.append(1)
            return real_extract(*args, **kwargs)

        with patch.dict("os.environ", worker_env), patch.object(
                durable_extraction, "extract_resume", side_effect=count_extract):
            result = execute_operation(operation_id)
            duplicate = execute_operation(operation_id)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotEqual(duplicate["status"], "COMPLETED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.profile(self.profile_id), before_profile)
        readback = self.client.get(
            f"/profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/queued/{operation_id}")
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["state"], "COMPLETED")
        facts = readback.json()["result"]["facts"]
        self.assertTrue(facts)
        self.assertTrue(all(fact["verified"] is False and fact["user_approved"] is False for fact in facts))
        self.assertEqual(self.client.get(f"/operations/{operation_id}").status_code, 200)
        generic_operation = self.client.get(f"/operations/{operation_id}").json()
        self.assertNotIn("Built durable local systems.", repr(generic_operation))
        self.assertEqual(self.client.get(
            f"/profiles/{self.other_profile_id}/resumes/{self.resume['id']}/extract/queued/{operation_id}"
        ).status_code, 404)
        upload_path = self.root / "uploads" / f"{self.resume['id']}.txt"
        upload_path.write_bytes(self.resume_bytes + b"Tampered source after completion.\n")
        self.assertEqual(self.client.get(
            f"/profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/queued/{operation_id}"
        ).status_code, 409)
        upload_path.write_bytes(self.resume_bytes)

    def test_process_crash_after_extraction_replays_and_commits_one_result(self):
        queued = self.queue()
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        child = r'''
import os, sys
from services.career_profile import durable_extraction
real_extract = durable_extraction.extract_resume
def extract_then_exit(*args, **kwargs):
    real_extract(*args, **kwargs)
    from pathlib import Path
    Path(os.environ["MJOBS_OUTPUT_DIR"], "extract-count").write_text("1", encoding="ascii")
    os._exit(73)
durable_extraction.extract_resume = extract_then_exit
from workers.tasks import execute_operation
execute_operation(sys.argv[1])
os._exit(74)
'''
        child_env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA")
            if key in os.environ
        }
        child_env.update({
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_OUTPUT_DIR": str(self.root),
            "PYTHONUTF8": "1",
        })
        repo_root = Path(__file__).resolve().parents[2]
        process = subprocess.run(
            [sys.executable, "-c", child, operation_id], cwd=repo_root,
            env=child_env, capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 73, process.stderr)
        self.assertEqual(self.operation(operation_id)["state"], "RUNNING")
        self.assertEqual(self.store.count("resume_extraction_results"), 0)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == operation_id).values(
                    lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
        self.assertEqual(recover_expired(self.store), 1)
        worker_env = dict(child_env)
        from services.career_profile import durable_extraction
        real_extract = durable_extraction.extract_resume

        def count_recovery_extract(*args, **kwargs):
            result = real_extract(*args, **kwargs)
            marker = self.root / "extract-count"
            marker.write_text(str(int(marker.read_text(encoding="ascii")) + 1), encoding="ascii")
            return result

        with patch.dict("os.environ", worker_env), patch.object(
                durable_extraction, "extract_resume", side_effect=count_recovery_extract):
            self.assertEqual(execute_operation(operation_id)["status"], "COMPLETED")
        self.assertEqual((self.root / "extract-count").read_text(encoding="ascii"), "2")
        self.assertEqual(self.store.count("resume_extraction_results"), 1)
        readback = self.client.get(
            f"/profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/queued/{operation_id}")
        self.assertEqual(readback.status_code, 200, readback.text)
        self.assertEqual(readback.json()["state"], "COMPLETED")

    def test_stale_worker_cannot_commit_after_expired_lease_is_reclaimed(self):
        queued = self.queue()
        operation_id = queued.json()["operation_id"]
        extracted = threading.Event()
        release = threading.Event()
        worker_result = {}
        worker_error = []
        from services.career_profile import durable_extraction
        real_extract = durable_extraction.extract_resume

        def pause_after_extraction(*args, **kwargs):
            result = real_extract(*args, **kwargs)
            extracted.set()
            if not release.wait(10):
                raise TimeoutError("test worker was not released")
            return result

        worker_env = {
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_OUTPUT_DIR": str(self.root),
        }

        def execute_stale_worker():
            try:
                worker_result.update(execute_operation(operation_id))
            except BaseException as error:
                worker_error.append(error)

        with patch.dict("os.environ", worker_env), patch.object(
                durable_extraction, "extract_resume", side_effect=pause_after_extraction):
            thread = threading.Thread(target=execute_stale_worker, daemon=True)
            thread.start()
            self.assertTrue(extracted.wait(10), "first worker did not reach result-before-commit seam")
            with self.store.engine.begin() as connection:
                connection.execute(update(m.operations).where(
                    m.operations.c.id == operation_id).values(
                        lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()))
            self.assertEqual(recover_expired(self.store), 1)
            second_claim = claim(self.store, operation_id)
            self.assertEqual(second_claim["attempt_count"], 2)
            release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(worker_error, [])
        self.assertEqual(worker_result["status"], "OWNERSHIP_EXPIRED")
        self.assertEqual(self.operation(operation_id)["state"], "RUNNING")
        self.assertEqual(self.operation(operation_id)["attempt_count"], 2)
        self.assertEqual(self.store.count("resume_extraction_results"), 0)

    def test_result_and_completed_state_roll_back_together_on_result_write_failure(self):
        queued = self.queue()
        operation_id = queued.json()["operation_id"]
        worker_env = {
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_OUTPUT_DIR": str(self.root),
        }
        with patch.dict("os.environ", worker_env), patch(
                "workers.tasks.persist_result", side_effect=RuntimeError("injected commit failure")):
            with self.assertRaisesRegex(RuntimeError, "injected commit failure"):
                execute_operation(operation_id)
        self.assertEqual(self.operation(operation_id)["state"], "RUNNING")
        self.assertEqual(self.store.count("resume_extraction_results"), 0)

    def test_changed_file_is_rejected_and_queued_cancel_never_publishes_facts(self):
        upload_path = self.root / "uploads" / f"{self.resume['id']}.txt"
        upload_path.write_bytes(self.resume_bytes + b"Changed after upload.\n")
        changed = self.queue()
        self.assertEqual(changed.status_code, 409, changed.text)
        self.assertEqual(self.store.count("worker_operations"), 0)

        upload_path.write_bytes(self.resume_bytes)
        queued = self.queue()
        self.assertEqual(queued.status_code, 202, queued.text)
        operation_id = queued.json()["operation_id"]
        cancelled = self.client.post(
            f"/profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertTrue(cancelled.json()["cancelled"])
        self.assertEqual(self.operation(operation_id)["state"], "CANCELLED")
        self.assertEqual(self.store.count("resume_extraction_results"), 0)

    def test_terminal_intent_replays_unchanged_and_explicit_new_intent_can_retry(self):
        first = self.queue()
        self.assertEqual(first.status_code, 202, first.text)
        operation_id = first.json()["operation_id"]
        cancelled = self.client.post(
            f"/profiles/{self.profile_id}/resumes/{self.resume['id']}/extract/cancel")
        self.assertTrue(cancelled.json()["cancelled"])
        same_intent = self.queue()
        self.assertEqual(same_intent.status_code, 202, same_intent.text)
        self.assertEqual(same_intent.json()["operation_id"], operation_id)
        self.assertEqual(same_intent.json()["state"], "CANCELLED")
        retry_id = "retry-" + uuid4().hex
        retry = self.queue(request_id=retry_id)
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertNotEqual(retry.json()["operation_id"], operation_id)
        failed_id = retry.json()["operation_id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(m.operations.c.id == failed_id).values(
                state="DEAD_LETTERED", error="SYNTHETIC_FAILURE"))
        failed_replay = self.queue(request_id=retry_id)
        self.assertEqual(failed_replay.status_code, 202)
        self.assertEqual(failed_replay.json()["state"], "DEAD_LETTERED")
        self.assertEqual(failed_replay.json()["operation_id"], failed_id)
        another_retry = self.queue(request_id="retry-" + uuid4().hex)
        self.assertNotEqual(another_retry.json()["operation_id"], failed_id)
        self.assertEqual(self.store.count("worker_operations"), 3)

    def test_same_request_id_cannot_be_rebound_to_changed_resume_hash(self):
        first = self.queue()
        self.assertEqual(first.status_code, 202, first.text)
        from services.career_profile.durable_extraction import queue_extraction, DurableExtractionError
        changed = dict(self.resume, sha256="0" * 64)
        with self.assertRaisesRegex(DurableExtractionError, "RESUME_EXTRACTION_IDENTITY_CONFLICT"):
            queue_extraction(self.store, self.profile_id, changed, self.request_id)

    def test_identical_request_ids_are_independent_across_profiles(self):
        other_resume = save_resume(
            self.store, self.root, self.other_profile_id, "other.txt", "text/plain", self.resume_bytes)
        first = self.queue()
        second = self.queue(profile_id=self.other_profile_id, resume_id=other_resume["id"])
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertNotEqual(first.json()["operation_id"], second.json()["operation_id"])


if __name__ == "__main__":
    unittest.main()
