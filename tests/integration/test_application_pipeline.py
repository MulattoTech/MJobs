"""User-reported application progress and notes use append-only scoped history."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-application-pipeline-")
        self.root = Path(self.temp.name)
        self.database = self.root / "trial.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.application_id = prepare(self.store, self.job_id, "fixture", self.root)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        self.url = f"/profiles/fixture/applications/{self.application_id}/tracking"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_user_reported_status_and_notes_survive_reopen_without_submission_receipt(self):
        before = self.store.application(self.application_id)
        initial = self.client.get(self.url)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json(), {"status": None, "attribution": None,
                                          "notes": [], "history": []})

        applied = self.client.put(self.url + "/status", json={
            "status": "APPLIED", "request_id": "user-reported-applied"})
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertEqual(applied.json()["status"], "APPLIED")
        self.assertEqual(applied.json()["attribution"], "USER_REPORTED")
        replay = self.client.put(self.url + "/status", json={
            "status": "APPLIED", "request_id": "user-reported-applied"})
        self.assertEqual(replay.json(), applied.json())
        self.assertEqual(len(replay.json()["history"]), 1)

        note_payload = {"text": "Synthetic note: emailed recruiter; awaiting reply.",
                        "request_id": "application-note-1"}
        note = self.client.post(self.url + "/notes", json=note_payload)
        self.assertEqual(note.status_code, 201, note.text)
        self.assertEqual(note.json()["notes"][0]["text"], note_payload["text"])
        self.assertEqual(self.client.post(self.url + "/notes", json=note_payload).json(),
                         note.json())
        self.assertEqual([row["sequence"] for row in note.json()["history"]], [1, 2])

        self.assertEqual(self.client.get(
            f"/profiles/other/applications/{self.application_id}/tracking").status_code, 404)
        self.assertEqual(self.client.put(
            f"/profiles/other/applications/{self.application_id}/tracking/status",
            json={"status": "OFFER", "request_id": "foreign-status"}).status_code, 404)
        self.assertEqual(self.client.post(
            f"/profiles/other/applications/{self.application_id}/tracking/notes",
            json={"text": "must not leak", "request_id": "foreign-note"}).status_code, 404)
        self.assertEqual(self.client.get(
            f"/profiles/missing/applications/{self.application_id}/tracking").status_code, 404)
        unchanged = self.store.application(self.application_id)
        self.assertEqual(unchanged["state"], before["state"])
        self.assertEqual(unchanged["confirmation"], before["confirmation"])
        self.assertIsNone(unchanged["confirmation"])
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit).where(
                m.audit.c.action.in_(("APPLICATION_PIPELINE_STATUS", "APPLICATION_NOTE"))
            )).mappings())
        self.assertEqual([row["action"] for row in sorted(events, key=lambda row: row["payload"]["sequence"])],
                         ["APPLICATION_PIPELINE_STATUS", "APPLICATION_NOTE"])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        reopened = self.client.get(self.url)
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["status"], "APPLIED")
        self.assertEqual(reopened.json()["attribution"], "USER_REPORTED")
        self.assertEqual(reopened.json()["notes"][0]["text"], note_payload["text"])
        self.assertEqual([row["sequence"] for row in reopened.json()["history"]], [1, 2])
        self.assertIsNone(self.store.application(self.application_id)["confirmation"])

    def test_invalid_pipeline_status_and_empty_note_write_no_history(self):
        invalid = self.client.put(self.url + "/status", json={
            "status": "SUBMISSION_CONFIRMED", "request_id": "invalid-status"})
        self.assertEqual(invalid.status_code, 422)
        empty = self.client.post(self.url + "/notes", json={
            "text": "  ", "request_id": "empty-note"})
        self.assertEqual(empty.status_code, 422)
        with self.store.engine.connect() as connection:
            rows = connection.execute(select(m.audit.c.id).where(
                m.audit.c.action.in_(("APPLICATION_PIPELINE_STATUS", "APPLICATION_NOTE"))
            )).all()
        self.assertEqual(rows, [])

    def test_supported_manual_stages_are_listed_and_request_ids_cannot_change_meaning(self):
        stages = ["SAVED", "READY", "APPLIED", "RECRUITER_CONTACT", "INTERVIEW",
                  "TECHNICAL_INTERVIEW", "FINAL_INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN"]
        for index, status in enumerate(stages):
            response = self.client.put(self.url + "/status", json={
                "status": status, "request_id": f"stage-{index}"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], status)
        listed = self.client.get("/applications?profile_id=fixture")
        self.assertEqual(listed.status_code, 200, listed.text)
        row = next(item for item in listed.json()["applications"]
                   if item["id"] == self.application_id)
        self.assertEqual(row["pipeline_status"], "WITHDRAWN")
        self.assertEqual(row["pipeline_attribution"], "USER_REPORTED")

        conflict = self.client.put(self.url + "/status", json={
            "status": "OFFER", "request_id": "stage-0"})
        self.assertEqual(conflict.status_code, 422)
        note_conflict = self.client.post(self.url + "/notes", json={
            "text": "First note", "request_id": "same-event"})
        self.assertEqual(note_conflict.status_code, 201, note_conflict.text)
        reused = self.client.post(self.url + "/notes", json={
            "text": "Changed note", "request_id": "same-event"})
        self.assertEqual(reused.status_code, 422)
        final = self.client.get(self.url)
        self.assertEqual(final.json()["status"], "WITHDRAWN")
        self.assertEqual([row["text"] for row in final.json()["notes"]], ["First note"])

    def test_mock_actions_are_profile_scoped(self):
        url = f"/profiles/fixture/applications/{self.application_id}/mock-run"
        foreign = self.client.post(
            f"/profiles/other/applications/{self.application_id}/mock-run",
            json={"submit": False})
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(self.store.count("worker_operations"), 0)
        own = self.client.post(url, json={"submit": False})
        self.assertEqual(own.status_code, 202, own.text)
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_concurrent_updates_allocate_a_single_ordered_history(self):
        barrier = Barrier(3)

        def update(status, request_id):
            barrier.wait(timeout=5)
            return self.client.put(self.url + "/status", json={
                "status": status, "request_id": request_id})

        updates = [("SAVED", "parallel-saved"), ("INTERVIEW", "parallel-interview"),
                   ("OFFER", "parallel-offer")]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(update, status, request_id)
                       for status, request_id in updates]
            responses = [future.result(timeout=10) for future in futures]
        self.assertTrue(all(response.status_code == 200 for response in responses),
                        [response.text for response in responses])
        history = self.client.get(self.url).json()["history"]
        self.assertEqual([item["sequence"] for item in history], [1, 2, 3])
        self.assertEqual({item["status"] for item in history},
                         {"SAVED", "INTERVIEW", "OFFER"})
        self.assertEqual(self.client.get(self.url).json()["status"], history[-1]["status"])


if __name__ == "__main__":
    unittest.main()
