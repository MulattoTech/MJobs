"""Profile-scoped job disposition and notes use append-only audit history."""
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
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

        saved = self.client.put(url + "/disposition", json={"disposition": "SAVED"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["disposition"], "SAVED")
        note = self.client.post(url + "/notes", json={"text": "Ask about weekend on-call rotation."})
        self.assertEqual(note.status_code, 201, note.text)
        self.assertEqual(note.json()["notes"][0]["text"], "Ask about weekend on-call rotation.")
        not_interested = self.client.put(url + "/disposition", json={
            "disposition": "NOT_INTERESTED"
        })
        self.assertEqual(not_interested.json()["disposition"], "NOT_INTERESTED")
        hidden = self.client.put(url + "/disposition", json={"disposition": "HIDDEN"})
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
        self.assertEqual([event.get("disposition") for event in reopened["history"]
                          if event["type"] == "disposition"], ["SAVED", "NOT_INTERESTED", "HIDDEN"])

    def test_invalid_disposition_and_empty_note_are_rejected_without_audit_writes(self):
        url = f"/profiles/fixture/jobs/{self.job_id}/tracking"
        self.assertEqual(self.client.put(url + "/disposition", json={
            "disposition": "SUBMITTED"
        }).status_code, 422)
        self.assertEqual(self.client.post(url + "/notes", json={"text": "  "}).status_code, 422)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.audit.c.id).where(
                m.audit.c.action.in_(("JOB_DISPOSITION", "JOB_NOTE")))).all(), [])


if __name__ == "__main__":
    unittest.main()
