"""Profile-scoped job disposition and notes use append-only audit history."""
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
from packages.persistence.store import Store, identity, now
from services.discovery.pipeline import discover
from services.tracking.jobs import set_job_disposition
from tests.integration.test_spine import PROFILE


class JobTrackingApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-job-tracking-")
        self.root = Path(self.directory.name)
        self.store = Store(f"sqlite:///{(self.root / 'trial.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.store.save_profile({**PROFILE, "id": "other", "name": "Other Profile",
                                 "email": "other@example.invalid"})
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.job_id = self.store.jobs("fixture")[0]["id"]
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.directory.cleanup()

    def test_disposition_and_notes_are_profile_scoped_persistent_and_append_only(self):
        url = f"/profiles/fixture/jobs/{self.job_id}/tracking"
        other_url = f"/profiles/other/jobs/{self.job_id}/tracking"
        initial = self.client.get(url)
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertEqual(initial.json(), {"disposition": "ACTIVE", "notes": [], "history": []})

        saved_payload = {"disposition": "SAVED", "request_id": "save-first"}
        saved = self.client.put(url + "/disposition", json=saved_payload)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["disposition"], "SAVED")
        self.assertEqual(self.client.put(url + "/disposition", json=saved_payload).json(),
                         saved.json())
        note_payload = {"text": "Ask about weekend on-call rotation.",
                        "request_id": "note-first"}
        note = self.client.post(url + "/notes", json=note_payload)
        self.assertEqual(note.status_code, 201, note.text)
        self.assertEqual(note.json()["notes"][0]["text"], "Ask about weekend on-call rotation.")
        replay = self.client.post(url + "/notes", json=note_payload)
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), note.json())
        self.assertEqual(len(replay.json()["notes"]), 1)
        self.assertEqual(self.client.put(url + "/disposition", json={
            "disposition": "HIDDEN", "request_id": "save-first"
        }).status_code, 422)
        self.assertEqual(self.client.post(url + "/notes", json={
            "text": "Different text", "request_id": "note-first"
        }).status_code, 422)
        not_interested = self.client.put(url + "/disposition", json={
            "disposition": "NOT_INTERESTED", "request_id": "not-interested"
        })
        self.assertEqual(not_interested.json()["disposition"], "NOT_INTERESTED")
        hidden = self.client.put(url + "/disposition", json={
            "disposition": "HIDDEN", "request_id": "hidden"
        })
        self.assertEqual(hidden.json()["disposition"], "HIDDEN")

        jobs = self.client.get("/jobs", params={"profile_id": "fixture"}).json()["jobs"]
        self.assertNotIn(self.job_id, [row["id"] for row in jobs])
        dismissed = self.client.get("/jobs", params={"profile_id": "fixture", "include_dismissed": "true"}).json()["jobs"]
        job = next(row for row in dismissed if row["id"] == self.job_id)
        self.assertEqual(job["tracking"], {"disposition": "HIDDEN"})
        detail = self.client.get(f"/jobs/{self.job_id}", params={"profile_id": "fixture"}).json()
        self.assertEqual(detail["tracking"]["notes"][0]["text"], "Ask about weekend on-call rotation.")

        other = self.client.get(other_url)
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["disposition"], "ACTIVE")
        self.assertEqual(other.json()["notes"], [])
        self.assertEqual(other.json()["history"], [])
        foreign = self.client.get(f"/profiles/missing/jobs/{self.job_id}/tracking")
        self.assertEqual(foreign.status_code, 404)
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit).where(m.audit.c.action.in_(
                ("JOB_DISPOSITION", "JOB_NOTE")))).mappings())
        self.assertEqual(len(events), 4)
        self.assertEqual(sorted(event["action"] for event in events),
                         ["JOB_DISPOSITION", "JOB_DISPOSITION", "JOB_DISPOSITION", "JOB_NOTE"])

        self.client.close()
        self.store.engine.dispose()
        self.store = Store(f"sqlite:///{(self.root / 'trial.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        reopened = self.client.get(url).json()
        self.assertEqual(reopened["disposition"], "HIDDEN")
        self.assertEqual(reopened["notes"][0]["text"], "Ask about weekend on-call rotation.")
        disposition_history = [event for event in reopened["history"]
                               if event["type"] == "disposition"]
        self.assertEqual([event["disposition"] for event in disposition_history],
                         ["SAVED", "NOT_INTERESTED", "HIDDEN"])
        self.assertEqual([event["sequence"] for event in reopened["history"]], [1, 2, 3, 4])

    def test_invalid_disposition_and_empty_note_are_rejected_without_audit_writes(self):
        url = f"/profiles/fixture/jobs/{self.job_id}/tracking"
        self.assertEqual(self.client.put(url + "/disposition", json={
            "disposition": "SUBMITTED", "request_id": "invalid-disposition"
        }).status_code, 422)
        self.assertEqual(self.client.post(url + "/notes", json={
            "text": "  ", "request_id": "empty-note"
        }).status_code, 422)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action.in_(("JOB_DISPOSITION", "JOB_NOTE")))).all(), [])

    def test_reads_pre_sequence_tracking_history_and_appends_new_events(self):
        entity_id = identity("job-tracking", "fixture", self.job_id)
        with self.store.engine.begin() as connection:
            connection.execute(m.audit.insert(), [
                {"id": "legacy-disposition", "entity_id": entity_id,
                 "action": "JOB_DISPOSITION", "payload": {"disposition": "SAVED"},
                 "timestamp": now()},
                {"id": "legacy-note", "entity_id": entity_id,
                 "action": "JOB_NOTE", "payload": {"note_id": "legacy-note-id",
                 "text": "Saved before the sequence upgrade."}, "timestamp": now()},
            ])
        url = f"/profiles/fixture/jobs/{self.job_id}/tracking"
        legacy = self.client.get(url)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.json()["disposition"], "SAVED")
        self.assertEqual(legacy.json()["notes"][0]["text"],
                         "Saved before the sequence upgrade.")
        self.assertEqual([row["sequence"] for row in legacy.json()["history"]], [0, 0])

        updated = self.client.put(url + "/disposition", json={
            "disposition": "HIDDEN", "request_id": "post-upgrade-hide"
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["disposition"], "HIDDEN")
        self.assertEqual([row["sequence"] for row in updated.json()["history"]], [0, 0, 1])

    def test_concurrent_disposition_updates_get_serial_history(self):
        barrier = Barrier(2)

        def update(disposition, request_id):
            barrier.wait(timeout=5)
            return set_job_disposition(self.store, "fixture", self.job_id,
                                       disposition, request_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(update, "SAVED", "concurrent-save"),
                       pool.submit(update, "HIDDEN", "concurrent-hide")]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual({result["disposition"] for result in results},
                         {"SAVED", "HIDDEN"})
        final = self.client.get(f"/profiles/fixture/jobs/{self.job_id}/tracking").json()
        statuses = [row for row in final["history"] if row["type"] == "disposition"]
        self.assertEqual([row["sequence"] for row in statuses], [1, 2])
        self.assertEqual(final["disposition"], statuses[-1]["disposition"])


if __name__ == "__main__":
    unittest.main()
