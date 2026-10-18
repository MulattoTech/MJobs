"""Explicit stale-application reprepare preserves immutable revisions."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from services.documents.history import application_documents
from services.applications.preflight import persist_preflight
from services.applications.attempt_history import AttemptConflict
from services.applications.run_requests import (
    queue_mock_run, read_mock_run, validate_run_operation,
)
from tests.integration.test_spine import PROFILE


class ApplicationReprepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-application-reprepare-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'reprepare.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.profile = deepcopy(PROFILE)
        self.profile["answers"] = []
        self.store.save_profile(self.profile)
        other = deepcopy(PROFILE)
        other.update(id="other-profile", name="Other synthetic applicant",
                     email="other@example.invalid", answers=[])
        self.store.save_profile(other)
        with fixture_server() as fixture:
            discover(self.store, fixture, "fixture")
        self.job = next(row for row in self.store.jobs("fixture")
                        if row.get("requisition") == "B")
        self.application_id = prepare(self.store, self.job["id"], "fixture", self.root)
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="FIXTURE",
        ), raise_server_exceptions=False)
        self.url = f"/profiles/fixture/applications/{self.application_id}/reprepare"

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _payload(self, *, app=None, profile=None):
        app = app or self.store.application(self.application_id)
        profile = profile or self.store.profile("fixture")
        return {
            "expected_document_version": app["document_version"],
            "expected_application_profile_version": app["profile_version"],
            "expected_profile_version": profile["version"],
            "expected_state": app["state"],
        }

    def test_explicit_reprepare_keeps_old_revision_audits_transition_and_is_idempotent(self):
        old_app = self.store.application(self.application_id)
        old_documents = application_documents(self.store, self.application_id, self.root)
        persist_preflight(self.store, self.application_id, {
            "result": "FAIL", "reason": "UNRESOLVED_REQUIRED_QUESTIONS",
            "observed_at": "2026-10-18T00:00:00Z",
            "profile_version": old_app["profile_version"],
            "document_version": old_app["document_version"],
            "route_id": old_app["route_id"], "provider": "mock-ats",
            "tenant": "fixture", "requisition": "B", "metadata_fingerprint": "b" * 64,
            "mappings": [], "missing": [],
        })
        current = self.store.profile("fixture")
        self.store.save_profile({**current, "answers": [{
            "id": "reviewed-answer", "question": "Describe a synthetic review.",
            "text": "Reviewed synthetic answer.", "source": "User-reviewed fixture",
            "verified": True, "user_approved": True, "confidence": 90,
        }]})
        latest = self.store.profile("fixture")
        payload = self._payload(app=old_app, profile=latest)
        response = self.client.post(self.url, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        refreshed = response.json()
        self.assertEqual(refreshed["id"], self.application_id)
        self.assertEqual(refreshed["profile_version"], latest["version"])
        self.assertEqual(refreshed["document_version"], old_app["document_version"] + 1)
        self.assertIsNone(refreshed["question_preflight"])

        all_documents = application_documents(self.store, self.application_id, self.root)
        self.assertTrue(set(row["id"] for row in old_documents) <=
                        set(row["id"] for row in all_documents))
        old_revision = [row for row in all_documents
                        if row["version"] == old_app["document_version"]]
        self.assertEqual({row["profile_version"] for row in old_revision},
                         {old_app["profile_version"]})
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            ).order_by(m.audit.c.timestamp, m.audit.c.id)).mappings())
        transition = events[-1]["payload"].get("reprepare")
        self.assertEqual(transition["prior"]["profile_version"], old_app["profile_version"])
        self.assertEqual(transition["prior"]["document_version"], old_app["document_version"])
        self.assertEqual(transition["prior"]["route_id"], old_app["route_id"])
        self.assertEqual(transition["current"]["profile_version"], latest["version"])

        event_count = len(events)
        documents_before_repeat = len(all_documents)
        repeated = self.client.post(self.url, json=self._payload())
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["document_version"], refreshed["document_version"])
        self.assertEqual(len(application_documents(self.store, self.application_id, self.root)),
                         documents_before_repeat)
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(select(m.audit.c.id).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "DOCUMENTS_PREPARED",
            )).all().__len__(), event_count)

    def test_stale_cross_profile_and_inflight_reprepare_fail_closed(self):
        app = self.store.application(self.application_id)
        profile = self.store.profile("fixture")
        wrong_application_version = self.client.post(self.url, json={
            **self._payload(app=app, profile=profile),
            "expected_document_version": app["document_version"] + 10,
        })
        self.assertEqual(wrong_application_version.status_code, 409)
        other = self.client.post(
            f"/profiles/other-profile/applications/{self.application_id}/reprepare",
            json=self._payload(app=app, profile=profile),
        )
        self.assertEqual(other.status_code, 404)

        changed = deepcopy(profile)
        changed["name"] = "Concurrent synthetic profile edit"
        self.store.save_profile(changed)
        current = self.store.profile("fixture")
        stale_profile = self.client.post(self.url, json=self._payload(
            app=app, profile=profile))
        self.assertEqual(stale_profile.status_code, 409)
        self.assertEqual(current["version"], self.store.profile("fixture")["version"])

        with self.store.engine.begin() as connection:
            connection.execute(m.attempts.insert().values(
                id="synthetic-running-attempt", application_id=self.application_id,
                state="RUNNING", started_at="2026-10-18T00:00:00Z", steps=[],
            ))
        active = self.client.post(self.url, json=self._payload(app=app, profile=current))
        self.assertEqual(active.status_code, 409)
        self.assertEqual(self.store.application(self.application_id)["document_version"],
                         app["document_version"])

    def test_completed_old_run_receipt_cannot_certify_reprepared_revision(self):
        old_app = self.store.application(self.application_id)
        persist_preflight(self.store, self.application_id, {
            "result": "PASS", "reason": "SYNTHETIC_OLD_REVISION_PASS",
            "observed_at": "2026-10-18T00:00:00Z",
            "profile_version": old_app["profile_version"],
            "document_version": old_app["document_version"],
            "route_id": old_app["route_id"], "provider": "mock-ats",
            "tenant": "fixture", "requisition": "B",
            "metadata_fingerprint": "c" * 64,
            "mappings": [], "missing": [],
        })
        old_receipt = queue_mock_run(
            self.store, "fixture", self.application_id, submit=False,
            request_id="old-completed-dryrun",
        )
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == old_receipt["operation_id"]
            ).values(state="RUNNING"))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id
            ).values(state="DRY_RUN_COMPLETE"))
            connection.execute(update(m.operations).where(
                m.operations.c.id == old_receipt["operation_id"]
            ).values(state="COMPLETED"))

        completed_app = self.store.application(self.application_id)
        current = self.store.profile("fixture")
        self.store.save_profile({**current, "answers": [{
            "id": "new-reviewed-answer", "question": "Synthetic current question?",
            "text": "Current approved synthetic response.",
            "source": "User-reviewed fixture", "verified": True,
            "user_approved": True, "confidence": 90,
        }]})
        latest = self.store.profile("fixture")
        resumed = self.client.post(self.url, json=self._payload(
            app=completed_app, profile=latest))
        self.assertEqual(resumed.status_code, 200, resumed.text)
        current_app = resumed.json()
        self.assertEqual(current_app["state"], "PREPARED")
        self.assertGreater(current_app["document_version"], old_app["document_version"])
        self.assertEqual(current_app["profile_version"], latest["version"])
        self.assertIsNone(current_app["question_preflight"])

        # The terminal old receipt remains readable with its original fingerprint,
        # while replaying that identity against new pins is rejected.
        old_readback = read_mock_run(
            self.store, "fixture", self.application_id, "old-completed-dryrun",
        )
        self.assertEqual(old_readback["state"], "COMPLETED")
        self.assertEqual(old_readback["input_fingerprint"],
                         old_receipt["input_fingerprint"])
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_REQUEST_REUSED"):
            queue_mock_run(
                self.store, "fixture", self.application_id, submit=False,
                request_id="old-completed-dryrun",
            )

        # A new operation freezes the current profile/document pins and must run
        # to create a replacement preflight; no old receipt is reused.
        new_run = queue_mock_run(
            self.store, "fixture", self.application_id, submit=False,
            request_id="new-revision-dryrun",
        )
        self.assertNotEqual(new_run["input_fingerprint"], old_receipt["input_fingerprint"])
        with self.store.engine.connect() as connection:
            new_operation = connection.execute(select(m.operations).where(
                m.operations.c.id == new_run["operation_id"],
            )).mappings().one()
        self.assertEqual(new_operation["params"]["input_snapshot"]["profile_version"],
                         latest["version"])
        self.assertEqual(new_operation["params"]["input_snapshot"]["document_version"],
                         current_app["document_version"])
        validate_run_operation(self.store, dict(new_operation))
        self.assertIsNone(self.store.application(self.application_id)["confirmation"])
        self.assertIsNone(self.client.get(
            f"/profiles/fixture/applications/{self.application_id}"
        ).json()["question_preflight"])

    def test_generation_failure_keeps_old_pins_documents_and_audit(self):
        old_app = self.store.application(self.application_id)
        old_documents = application_documents(self.store, self.application_id, self.root)
        profile = self.store.profile("fixture")
        self.store.save_profile({**profile, "name": "Updated synthetic name"})
        latest = self.store.profile("fixture")
        old_audits = self.store.count("audit_events")
        with patch("services.documents.generate.publish_directory",
                   side_effect=OSError("synthetic publication failure")):
            response = self.client.post(self.url, json=self._payload(
                app=old_app, profile=latest))
        self.assertEqual(response.status_code, 500)
        current = self.store.application(self.application_id)
        self.assertEqual(current["profile_version"], old_app["profile_version"])
        self.assertEqual(current["document_version"], old_app["document_version"])
        self.assertEqual(application_documents(self.store, self.application_id, self.root),
                         old_documents)
        self.assertEqual(self.store.count("audit_events"), old_audits)


if __name__ == "__main__":
    unittest.main()
