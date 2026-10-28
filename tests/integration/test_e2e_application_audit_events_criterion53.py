"""Criterion 53: confirmation reconciliation remains visible in Activity."""

from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.confirmation_recovery import ACTION, reconcile_confirmation
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class ApplicationAuditEventsCriterion53Tests(unittest.TestCase):
    def test_confirmation_check_survives_reopen_as_safe_scoped_activity(self):
        with tempfile.TemporaryDirectory(prefix="mjobs-criterion53-audit-") as directory:
            root = Path(directory)
            database = root / "criterion53.db"
            store = Store(f"sqlite:///{database.as_posix()}")
            store.migrate()
            store.bind_workspace_mode("FIXTURE")
            store.save_profile(dict(PROFILE))
            store.save_profile({
                **PROFILE,
                "id": "other-profile",
                "name": "Other Private Person",
                "email": "other-private@example.invalid",
            })

            with fixture_server() as origin:
                discover(store, origin, "fixture")
                discover(store, origin, "other-profile")
                fixture_application = prepare(
                    store, store.jobs("fixture")[0]["id"], "fixture", root,
                )
                other_application = prepare(
                    store, store.jobs("other-profile")[0]["id"], "other-profile", root,
                )
                store.transition(
                    fixture_application, ["PREPARED"], "SUBMISSION_UNCONFIRMED",
                )
                store.transition(
                    other_application, ["PREPARED"], "SUBMISSION_UNCONFIRMED",
                )

                private_request_id = "criterion53-private-token"
                result = reconcile_confirmation(
                    store,
                    "fixture",
                    fixture_application,
                    origin,
                    private_request_id,
                    operation_mode="FIXTURE",
                )
                replay = reconcile_confirmation(
                    store,
                    "fixture",
                    fixture_application,
                    origin,
                    private_request_id,
                    operation_mode="FIXTURE",
                )

            self.assertEqual(result["status"], "NO_VERIFIED_RECEIPT")
            self.assertEqual(replay, {**result, "replayed": True})
            with store.engine.connect() as connection:
                rows = connection.execute(select(
                    m.audit.c.id, m.audit.c.entity_id, m.audit.c.action,
                ).where(m.audit.c.action == ACTION)).mappings().all()
            self.assertEqual(rows, [{
                "id": result["check_id"],
                "entity_id": fixture_application,
                "action": ACTION,
            }])
            store.engine.dispose()

            reopened = Store(f"sqlite:///{database.as_posix()}")
            client = TestClient(create_app(
                reopened,
                "http://127.0.0.1:1",
                root,
                "redis://127.0.0.1:1/0",
                operation_mode="FIXTURE",
            ))
            try:
                response = client.get("/profiles/fixture/audit-history")
                self.assertEqual(response.status_code, 200, response.text)
                events = response.json()["events"]
                confirmation_events = [
                    event for event in events
                    if event["type"] == "APPLICATION_CONFIRMATION_CHECKED"
                ]
                self.assertEqual(len(confirmation_events), 1, response.text)
                event = confirmation_events[0]
                self.assertEqual(event, {
                    "id": result["check_id"],
                    "type": "APPLICATION_CONFIRMATION_CHECKED",
                    "summary": "Confirmation check found no verified receipt",
                    "subject": "Historical details not recorded",
                    "company": "",
                    "timestamp": result["checked_at"],
                })
                self.assertEqual(
                    [(row["timestamp"], row["id"]) for row in events],
                    sorted(
                        ((row["timestamp"], row["id"]) for row in events),
                        reverse=True,
                    ),
                )
                self.assertEqual(
                    set(event),
                    {"id", "type", "summary", "subject", "company", "timestamp"},
                )

                other = client.get("/profiles/other-profile/audit-history")
                self.assertEqual(other.status_code, 200, other.text)
                self.assertFalse(any(
                    row["id"] == result["check_id"]
                    or row["type"] == "APPLICATION_CONFIRMATION_CHECKED"
                    for row in other.json()["events"]
                ))

                for private in (
                    PROFILE["name"],
                    PROFILE["email"],
                    "Other Private Person",
                    "other-private@example.invalid",
                    private_request_id,
                    origin,
                    fixture_application,
                    other_application,
                    ACTION,
                    "route_identity",
                    "provider_receipt_verified",
                ):
                    self.assertNotIn(private, response.text)
            finally:
                client.close()
                reopened.engine.dispose()


if __name__ == "__main__":
    unittest.main()
