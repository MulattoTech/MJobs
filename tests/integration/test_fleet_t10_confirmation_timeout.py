"""Team 10 timeout evidence remains explicit and durable across reloads."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
from sqlalchemy import select, update
from sqlalchemy.orm.exc import NoResultFound

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.confirmation_recovery import (
    ACTION, ConfirmationConflict, latest_confirmation_check, reconcile_confirmation,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class FleetT10ConfirmationTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t10-confirmation-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 'fixture.db').as_posix()}"
        self.store = Store(self.database_url)
        self.addCleanup(lambda: self.store.engine.dispose())
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.fixture = fixture_server()
        self.origin = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        discover(self.store, self.origin, PROFILE["id"])
        self.application_id = prepare(
            self.store, self.store.jobs(PROFILE["id"])[0]["id"], PROFILE["id"], self.root)
        self.store.transition(self.application_id, ["PREPARED"], "SUBMISSION_UNCONFIRMED")
        self.endpoint = f"/profiles/{PROFILE['id']}/applications/{self.application_id}/confirmation-check"

    def test_timeout_is_durable_unconfirmed_evidence_after_store_reopen(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id).values(state="SUBMITTING"))
        with TestClient(create_app(
            self.store, self.origin, self.root, operation_mode="FIXTURE"
        )) as client, patch(
            "services.discovery.pipeline.MockClient.get", side_effect=httpx.ReadTimeout("synthetic timeout")
        ):
            response = client.post(self.endpoint, json={"request_id": "t10-timeout-reload"})

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "CHECK_UNAVAILABLE")
        self.assertEqual(result["lookup_failure_code"], "CONFIRMATION_READ_TIMEOUT")
        self.assertEqual(result["application_state"], "SUBMISSION_UNCONFIRMED")
        self.assertFalse(result["provider_receipt_verified"])
        self.assertFalse(result["browser_started"])
        self.assertFalse(result["submission_attempted"])
        self.assertEqual(
            httpx.get(self.origin + "/_test/request-counts", trust_env=False).json()["submit"], 0)
        self.assertEqual(self.store.application(self.application_id)["state"], "SUBMISSION_UNCONFIRMED")
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(m.audit.c.id).where(m.audit.c.action == ACTION)
            ).scalars().all(), [result["check_id"]])

        self.store.engine.dispose()
        self.store = Store(self.database_url)
        with TestClient(create_app(
            self.store, self.origin, self.root, operation_mode="FIXTURE"
        )) as reopened_client:
            replay = reopened_client.post(self.endpoint, json={"request_id": "t10-timeout-reload"})

        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), {**result, "replayed": True})
        latest = latest_confirmation_check(
            self.store, PROFILE["id"], self.application_id, operation_mode="FIXTURE")
        self.assertEqual(latest, {**result, "replayed": True})
        self.assertEqual(self.store.application(self.application_id)["state"], "SUBMISSION_UNCONFIRMED")
        with self.store.engine.connect() as connection:
            self.assertEqual(connection.execute(
                select(m.audit.c.id).where(m.audit.c.action == ACTION)
            ).scalars().all(), [result["check_id"]])
        self.store.engine.dispose()

    def test_latest_check_is_mode_owned_and_bound_to_saved_route(self):
        result = reconcile_confirmation(
            self.store, PROFILE["id"], self.application_id, self.origin,
            "t10-latest-check", operation_mode="FIXTURE")
        self.assertEqual(result["status"], "NO_VERIFIED_RECEIPT")

        latest = latest_confirmation_check(
            self.store, PROFILE["id"], self.application_id, operation_mode="FIXTURE")
        self.assertEqual(latest, {**result, "replayed": True})
        with self.assertRaisesRegex(ConfirmationConflict, "FIXTURE_ONLY"):
            latest_confirmation_check(
                self.store, PROFILE["id"], self.application_id, operation_mode="PERSONAL")
        with self.assertRaises(NoResultFound):
            latest_confirmation_check(
                self.store, "another-profile", self.application_id, operation_mode="FIXTURE")

        route_id = self.store.application(self.application_id)["route_id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(m.routes.c.id == route_id).values(
                requisition="changed-requisition"))
        with self.assertRaisesRegex(ConfirmationConflict, "ROUTE_CHANGED"):
            latest_confirmation_check(
                self.store, PROFILE["id"], self.application_id, operation_mode="FIXTURE")

    def test_replay_refuses_legacy_check_without_saved_route_snapshot(self):
        result = reconcile_confirmation(
            self.store, PROFILE["id"], self.application_id, self.origin,
            "t10-legacy-route-snapshot", operation_mode="FIXTURE")
        with self.store.engine.begin() as connection:
            event = connection.execute(select(m.audit.c.id, m.audit.c.payload).where(
                m.audit.c.id == result["check_id"])).mappings().one()
            payload = dict(event["payload"])
            payload.pop("route_identity")
            connection.execute(update(m.audit).where(m.audit.c.id == event["id"]).values(
                payload=payload))

        with self.assertRaisesRegex(ConfirmationConflict, "ROUTE_CHANGED"):
            reconcile_confirmation(
                self.store, PROFILE["id"], self.application_id, self.origin,
                "t10-legacy-route-snapshot", operation_mode="FIXTURE")

    def test_timeout_preserves_prior_confirmation_but_marks_lookup_unavailable(self):
        self.store.transition(self.application_id, ["SUBMISSION_UNCONFIRMED"], "CONFIRMED",
                              confirmation={"reference_number": "MOCK-prior", "timestamp": "test"})
        with TestClient(create_app(
            self.store, self.origin, self.root, operation_mode="FIXTURE"
        )) as client, patch(
            "services.discovery.pipeline.MockClient.get", side_effect=httpx.ReadTimeout("synthetic timeout")
        ):
            response = client.post(self.endpoint, json={"request_id": "t10-confirmed-timeout"})

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(result["confirmation_source"], "PREVIOUS_CONFIRMATION")
        self.assertEqual(result["lookup_failure_code"], "CONFIRMATION_READ_TIMEOUT")
        self.assertFalse(result["provider_receipt_verified"])
        self.assertEqual(self.store.application(self.application_id)["state"], "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
