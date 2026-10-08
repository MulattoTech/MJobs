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


class ApplicationStepTraceTests(unittest.TestCase):
    """The mock attempt trace is durable, ordered, and contains no form values."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-application-step-trace-")
        self.root = Path(self.temp.name)
        self.database = self.root / "trace.db"
        self.store = Store(f"sqlite:///{self.database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def _attempt(self, store, application_id):
        with store.engine.connect() as connection:
            rows = list(connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == application_id
            ).order_by(m.attempts.c.started_at)).mappings())
        self.assertEqual(len(rows), 1)
        return dict(rows[0])

    def _prepare(self, origin):
        from services.discovery.pipeline import discover
        from services.documents.generate import prepare

        discover(self.store, origin, "fixture")
        job = next(row for row in self.store.jobs("fixture")
                   if row["requisition"] == "B")
        return prepare(self.store, job["id"], "fixture", self.root)

    def test_mock_attempt_records_all_phases_in_order_without_form_values_after_reopen(self):
        from fixtures.mock_server import fixture_server
        from services.applications.mock_adapter import apply_mock
        import json

        expected = [
            "NAVIGATE:COMPLETED", "DETECT_FORM:COMPLETED",
            "UPLOAD_RESUME:COMPLETED", "FILL_CONTACT:COMPLETED",
            "FILL_LOCATION:COMPLETED", "ANSWER_STANDARD:COMPLETED",
            "ANSWER_CUSTOM:COMPLETED", "VALIDATE:COMPLETED",
            "SUBMIT:COMPLETED", "VERIFY_CONFIRMATION:COMPLETED",
        ]
        with fixture_server() as origin:
            application_id = self._prepare(origin)
            result = apply_mock(self.store, application_id, origin, self.root, submit=True)
            self.assertEqual(result["state"], "CONFIRMED")
            self.assertEqual(self.store.application(application_id)["trace"][-1],
                             "VERIFY_CONFIRMATION")
            self.store.engine.dispose()
            reopened = Store(f"sqlite:///{self.database.as_posix()}")
            try:
                reopened.migrate()
                trace = self._attempt(reopened, application_id)["steps"]
                self.assertEqual(trace, expected)
                serialized = json.dumps(trace)
                private_values = [PROFILE["name"], PROFILE["email"]]
                private_values.extend(row["text"] for row in PROFILE.get("answers", []))
                self.assertTrue(all(value not in serialized for value in private_values if value),
                                "attempt trace must not echo candidate answer or contact values")
                with TestClient(create_app(reopened, "http://127.0.0.1:1", self.root,
                                           operation_mode="FIXTURE")) as client:
                    response = client.get(
                        f"/profiles/fixture/applications/{application_id}")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["attempts"][0]["steps"], expected)
                receipt = origin + "/ats/receipts/" + application_id
                self.assertEqual(__import__("httpx").get(receipt).json()["submission_count"], 1)
            finally:
                reopened.engine.dispose()

    def test_interrupted_contact_phase_is_persisted_as_failed_without_private_values(self):
        from fixtures.mock_server import fixture_server
        from playwright.sync_api import Locator
        from services.applications.mock_adapter import apply_mock
        from unittest.mock import patch
        import json

        with fixture_server() as origin:
            application_id = self._prepare(origin)
            original_fill = Locator.fill

            def interrupt_after_email(locator, value, *args, **kwargs):
                result = original_fill(locator, value, *args, **kwargs)
                if locator.get_attribute("name") == "email":
                    raise RuntimeError("PRIVATE_FAILURE synthetic answer=SECRET_SENTINEL")
                return result

            with patch.object(Locator, "fill", interrupt_after_email):
                with self.assertRaisesRegex(RuntimeError, "PRIVATE_FAILURE"):
                    apply_mock(self.store, application_id, origin, self.root)

            self.store.engine.dispose()
            reopened = Store(f"sqlite:///{self.database.as_posix()}")
            try:
                reopened.migrate()
                attempt = self._attempt(reopened, application_id)
                self.assertEqual(attempt["state"], "REVIEW_REQUIRED")
                self.assertIn("NAVIGATE:COMPLETED", attempt["steps"])
                self.assertIn("DETECT_FORM:COMPLETED", attempt["steps"])
                self.assertIn("UPLOAD_RESUME:COMPLETED", attempt["steps"])
                self.assertIn("FILL_CONTACT:FAILED", attempt["steps"])
                serialized = json.dumps(attempt["steps"])
                self.assertNotIn(PROFILE["email"], serialized)
                self.assertNotIn(PROFILE["name"], serialized)
                self.assertNotIn("SECRET_SENTINEL", serialized)
                self.assertEqual(__import__("httpx").get(
                    origin + "/ats/receipts/" + application_id
                ).json(), {"status": "not_found"})
            finally:
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
