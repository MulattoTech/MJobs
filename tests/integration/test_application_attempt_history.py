"""Durable admission, immutable finish, cross-profile/privacy and restart checks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store, now
from services.applications.attempt_history import (
    AttemptConflict, STARTED, FINISHED, start_attempt, finish_attempt, read_attempt_history,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class AttemptHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-attempt-history-")
        self.root = Path(self.temp.name)
        self.url = f"sqlite:///{(self.root / 'test.db').as_posix()}"
        self.store = Store(self.url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            self.origin = origin
            discover(self.store, origin, "fixture")
            self.app_id = prepare(self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.application = self.store.application(self.app_id)
        with self.store.engine.connect() as connection:
            self.route = dict(connection.execute(select(m.routes).where(
                m.routes.c.id == self.application["route_id"])).mappings().one())
            self.documents = [dict(row) for row in connection.execute(select(m.documents).where(
                m.documents.c.application_id == self.app_id)).mappings()]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def start(self, request_id="request-1", **kwargs):
        return start_attempt(self.store, self.application, self.route, self.documents,
                             request_id=request_id, **kwargs)

    def complete(self, identifier, error=None):
        self.store.transition(self.app_id, ["PREPARED", "DRY_RUN_COMPLETE"], "DRY_RUN_COMPLETE")
        finish_attempt(self.store, identifier, self.store.application(self.app_id), error=error)

    def history(self):
        return read_attempt_history(self.store, "fixture", self.app_id)

    def test_identity_snapshot_and_completion_survive_new_store(self):
        attempt = self.start()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.attempts).where(m.attempts.c.id == attempt["id"]).values(
                steps=["NAVIGATE:COMPLETED", "VALIDATE:COMPLETED"]))
        self.complete(attempt["id"])
        reopened = Store(self.url)
        try:
            row = read_attempt_history(reopened, "fixture", self.app_id)["attempts"][0]
        finally:
            reopened.engine.dispose()
        self.assertEqual(row["attempt_id"], attempt["id"])
        self.assertRegex(row["attempt_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(row["route_id"], self.application["route_id"])
        self.assertEqual(row["profile_version"], self.application["profile_version"])
        self.assertEqual(row["document_version"], self.application["document_version"])
        self.assertEqual(row["adapter"], "mock-ats")
        self.assertEqual(row["mode"], "DRY_RUN")
        self.assertEqual(row["current_step"], "VALIDATE")
        self.assertTrue(row["finished_at"])
        self.assertTrue(row["browser_session"])
        self.assertEqual(len(row["input_fingerprint"]), 64)
        self.assertFalse(row["legacy_metadata"])

    def test_successful_request_replay_creates_no_attempt_or_audit(self):
        first = self.start()
        self.complete(first["id"])
        counts = self.store.count("application_attempts"), self.store.count("audit_events")
        replay = self.start()
        self.assertEqual(replay, {"id": first["id"], "replayed": True})
        self.assertEqual(counts, (self.store.count("application_attempts"), self.store.count("audit_events")))

    def test_request_cannot_be_reused_for_different_submission_mode(self):
        first = self.start()
        self.complete(first["id"])
        with self.assertRaisesRegex(AttemptConflict, "REQUEST_REUSED"):
            self.start(submit=True)
        self.assertEqual(self.store.count("application_attempts"), 1)

    def test_parallel_requests_admit_only_one_browser_owner(self):
        def reserve(index):
            try:
                return self.start(f"parallel-{index}")["id"]
            except AttemptConflict as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, (1, 2)))
        self.assertEqual(results.count("APPLICATION_ATTEMPT_IN_PROGRESS"), 1)
        self.assertEqual(self.store.count("application_attempts"), 1)
        with self.assertRaisesRegex(AttemptConflict, "IN_PROGRESS"):
            self.start("another-request")

    def test_live_route_change_rejected_before_reservation(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == self.route["id"]).values(
                url=self.route["url"] + "?changed=1"))
        with self.assertRaisesRegex(AttemptConflict, "INPUT_CHANGED"):
            self.start()
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_live_document_change_rejected_before_reservation(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.documents).where(m.documents.c.id == self.documents[0]["id"]).values(
                sha256={"pdf": "0" * 64}))
        with self.assertRaisesRegex(AttemptConflict, "INPUT_CHANGED"):
            self.start()
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_version_change_rejected_before_reservation(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(m.applications.c.id == self.app_id).values(
                document_version=self.application["document_version"] + 1))
        with self.assertRaisesRegex(AttemptConflict, "INPUT_CHANGED"):
            self.start()
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_invalid_ids_and_nonmock_target_create_no_records(self):
        for request_id in ["", "a/b", "a b", "x" * 129, 123, False]:
            with self.subTest(request_id=request_id), self.assertRaises(AttemptConflict):
                self.start(request_id)
        with self.assertRaisesRegex(AttemptConflict, "TARGET_UNSUPPORTED"):
            start_attempt(self.store, {**self.application, "target": "REAL"}, self.route,
                          self.documents)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_failures_are_sanitized_and_require_an_explicit_new_request(self):
        first = self.start()
        finish_attempt(self.store, first["id"], self.application,
                       error=RuntimeError("PRIVATE_EMAIL secret-token C:/private/path"))
        row = self.history()["attempts"][0]
        self.assertEqual(row["state"], "FAILED")
        self.assertEqual(row["error_code"], "APPLICATION_EXECUTION_FAILED")
        self.assertNotIn("PRIVATE_EMAIL", json.dumps(row))
        with self.assertRaisesRegex(AttemptConflict, "REVIEW_REQUIRED"):
            self.start()
        self.assertFalse(self.start("explicit-new-request")["replayed"])

    def test_first_completion_is_immutable(self):
        first = self.start()
        self.complete(first["id"])
        original = self.history()
        finish_attempt(self.store, first["id"], {**self.application, "state": "FAILED"}, error=RuntimeError())
        self.assertEqual(self.history(), original)
        with self.store.engine.connect() as connection:
            actions = list(connection.execute(select(m.audit.c.action).where(
                m.audit.c.action.in_([STARTED, FINISHED]))).scalars())
        self.assertCountEqual(actions, [STARTED, FINISHED])

    def test_history_endpoint_is_read_only_scoped_and_hides_untrusted_steps(self):
        first = self.start()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.attempts).where(m.attempts.c.id == first["id"]).values(
                steps=["NAVIGATE:COMPLETED", "password=secret", "EMAIL:PRIVATE", "SUBMIT:<script>"]))
        app = create_app(self.store, self.origin, self.root, "redis://127.0.0.1:1/0", operation_mode="FIXTURE")
        client = TestClient(app)
        before = self.store.count("audit_events")
        url = f"/profiles/fixture/applications/{self.app_id}/attempt-history"
        response = client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["attempts"][0]["steps"], ["NAVIGATE:COMPLETED"])
        self.assertEqual(client.get(url.replace("/fixture/", "/other/")).status_code, 404)
        self.assertEqual(client.post(url).status_code, 405)
        self.assertEqual(self.store.count("audit_events"), before)
        for secret in [PROFILE["email"], PROFILE["name"], str(self.root), "password=secret"]:
            self.assertNotIn(secret, response.text)

    def test_legacy_history_has_no_fabricated_metadata_and_is_bounded(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert(), [dict(id=f"legacy-{i:03}",
                application_id=self.app_id, state="DRY_RUN_COMPLETE", steps=[], started_at=now())
                for i in range(101)])
        result = self.history()
        self.assertEqual(len(result["attempts"]), 100)
        self.assertTrue(result["has_more"])
        for row in result["attempts"]:
            self.assertTrue(row["legacy_metadata"])
            for key in ["route_id", "adapter", "browser_session", "finished_at", "input_fingerprint"]:
                self.assertIsNone(row[key])

    def test_unfinished_legacy_attempt_is_not_silently_stolen(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(id="legacy-running", application_id=self.app_id,
                state="RUNNING", steps=[], started_at="2000-01-01T00:00:00Z"))
        with self.assertRaisesRegex(AttemptConflict, "IN_PROGRESS"):
            self.start()
        self.assertEqual(self.store.count("application_attempts"), 1)
