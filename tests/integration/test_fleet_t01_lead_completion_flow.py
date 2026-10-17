"""Public user declaration, durable readback and fail-closed lifecycle."""
import json
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from tests.integration import test_application_form_review_flow as fixtures


class CompletionFlowTests(unittest.TestCase):
    _new_store = fixtures.ApplicationFormReviewFlowTests._new_store
    tearDown = fixtures.ApplicationFormReviewFlowTests.tearDown

    def setUp(self):
        fixtures.ApplicationFormReviewFlowTests.setUp(self)
        self.route_url = "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/US/Engineer_R1"
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(provider="workday", tenant="acme", requisition="R1", url=self.route_url))
        review = self.client.post(
            f"/profiles/fixture/applications/{self.application_id}/form-reviews",
            json={"request_id": "fleet-t01-completion"},
        )
        self.assertEqual(review.status_code, 202, review.text)
        self.operation_id = review.json()["operation_id"]
        self.path = (f"/profiles/fixture/applications/{self.application_id}"
                     f"/manual-auth/reviews/{self.operation_id}/completion")
        self.body = {"user_completed_authentication": True,
                     "expected_route_url": self.route_url, "expires_in_seconds": 900}

    def declare(self):
        response = self.client.put(self.path, json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["authenticated_session_available"])
        return response.json()

    def test_declaration_survives_store_recreation_without_new_attempt_or_operation(self):
        attempts = self.store.count("application_attempts")
        operations = self.store.count("worker_operations")
        declared = self.declare()
        audit_events = self.store.count("audit_events")
        self.assertEqual(self.declare(), declared)
        self.assertEqual(self.store.count("audit_events"), audit_events)
        self.assertEqual(self.client.get(self.path).json(), declared)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        restored = self.client.get(self.path)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json(), declared)
        self.assertEqual(self.store.count("application_attempts"), attempts)
        self.assertEqual(self.store.count("worker_operations"), operations)
        self.assertEqual(self.store.application(self.application_id)["state"], "PREPARED")
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == self.operation_id)).mappings().one()
        self.assertEqual(operation["state"], "QUEUED")
        serialized = json.dumps(declared)
        for private_key in ("cookies", "password", "access_token", "browser_profile"):
            self.assertNotIn(private_key, serialized)

    def test_route_profile_and_review_changes_cannot_reuse_declaration(self):
        self.declare()
        foreign = self.path.replace("/profiles/fixture/", "/profiles/foreign/")
        self.assertEqual(self.client.get(foreign).status_code, 404)
        profile = self.store.profile("fixture")
        profile["name"] = "Changed synthetic profile"
        self.store.save_profile(profile, expected_version=profile["version"])
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.put(self.path, json=self.body).status_code, 409)

    def test_revocation_is_durable_and_cannot_be_undone_by_readback(self):
        self.declare()
        revoked = self.client.delete(self.path)
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.client.close()
        self.store.engine.dispose()
        self.store = Store(self.database_url)
        self.client = TestClient(create_app(self.store, "http://127.0.0.1:1", self.root,
                                            operation_mode="FIXTURE"))
        restored = self.client.get(self.path)
        self.assertEqual(restored.status_code, 409, restored.text)

    def test_route_url_expectation_and_unowned_review_fail_closed(self):
        rejected = self.client.put(self.path, json={**self.body,
            "expected_route_url": "https://foreign.example/private"})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertNotIn("foreign.example", rejected.text)
        response = self.client.put(self.path.replace(self.operation_id, "foreign-review"), json=self.body)
        self.assertEqual(response.status_code, 404, response.text)

    def test_declaration_does_not_complete_required_authentication(self):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(state="AUTH_REQUIRED"))
        declared = self.declare()
        self.assertFalse(declared["authenticated_session_available"])
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        restored = self.client.get(self.path)
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertFalse(restored.json()["authenticated_session_available"])
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")

    def test_route_tenant_change_invalidates_restoration(self):
        self.declare()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(tenant="other"))
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(response.json().get("authenticated_session_available", False))

    def test_cancelled_review_cannot_restore_or_accept_declaration(self):
        self.declare()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.operation_id,
            ).values(cancel_requested=True))
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.client.put(self.path, json=self.body).status_code, 409)

    def test_supported_raw_route_uses_the_same_canonical_review_identity(self):
        raw_url = ("https://ACME.WD5.MYWORKDAYJOBS.COM/en-us/Acme/job/US/Engineer_R1"
                   "?source=PRIVATE_TRACKING")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(url=raw_url))
        admitted = self.client.post(
            f"/profiles/fixture/applications/{self.application_id}/form-reviews",
            json={"request_id": "fleet-t01-normalized-route"},
        )
        self.assertEqual(admitted.status_code, 202, admitted.text)
        self.operation_id = admitted.json()["operation_id"]
        self.path = (f"/profiles/fixture/applications/{self.application_id}"
                     f"/manual-auth/reviews/{self.operation_id}/completion")
        declared = self.declare()
        self.assertEqual(self.client.get(self.path).json(), declared)
        self.assertNotIn("PRIVATE_TRACKING", json.dumps(declared))
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit.c.payload).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "USER_AUTH_COMPLETION_DECLARED",
            )).scalars())
        self.assertNotIn("PRIVATE_TRACKING", json.dumps(events))
