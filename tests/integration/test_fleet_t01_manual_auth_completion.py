"""Metadata-only user auth declarations stay bound to a frozen Workday review."""
import unittest
from datetime import datetime, timedelta
import json
from unittest.mock import patch

from sqlalchemy import select, update

from packages.persistence import models as m
from tests.integration import test_application_form_review_flow as fixtures


class ManualAuthCompletionTests(unittest.TestCase):
    _new_store = fixtures.ApplicationFormReviewFlowTests._new_store
    tearDown = fixtures.ApplicationFormReviewFlowTests.tearDown

    def setUp(self):
        fixtures.ApplicationFormReviewFlowTests.setUp(self)
        self.route_url = (
            "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/US/Engineer_R1"
        )
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(self.application_id)["route_id"],
            ).values(provider="workday", tenant="acme", requisition="R1",
                     url=self.route_url))
        response = self.client.post(
            f"/profiles/fixture/applications/{self.application_id}/form-reviews",
            json={"request_id": "fleet-t01-service"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.review_id = response.json()["operation_id"]

    def record(self, *, expected_route_url=None, expires_in_seconds=900,
               review_operation_id=None):
        from services.applications.manual_auth import record_user_auth_completion

        return record_user_auth_completion(
            self.store, "fixture", self.application_id,
            review_operation_id or self.review_id,
            expected_route_url=expected_route_url or self.route_url,
            expires_in_seconds=expires_in_seconds,
        )

    def test_declaration_is_durable_idempotent_and_never_authenticates(self):
        from services.applications.manual_auth import current_user_auth_completion

        before_attempts = self.store.count("application_attempts")
        before_operations = self.store.count("worker_operations")
        first = self.record()
        second = self.record()
        restored = current_user_auth_completion(
            self.store, "fixture", self.application_id, self.review_id,
        )

        self.assertEqual(first, second)
        self.assertEqual(first, restored)
        self.assertEqual(first["status"], "USER_DECLARED")
        self.assertFalse(first["authenticated_session_available"])
        self.assertEqual(
            datetime.fromisoformat(first["expires_at"])
            - datetime.fromisoformat(first["declared_at"]),
            timedelta(seconds=900),
        )
        self.assertEqual(self.store.count("application_attempts"), before_attempts)
        self.assertEqual(self.store.count("worker_operations"), before_operations)
        with self.store.engine.connect() as connection:
            events = list(connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action == "USER_AUTH_COMPLETION_DECLARED",
            )).mappings())
        self.assertEqual(len(events), 1)
        serialized = json.dumps(events[0]["payload"])
        self.assertEqual(events[0]["payload"]["declaration"], "USER_DECLARED")
        for private_key in ("cookies", "password", "access_token", "browser_profile"):
            self.assertNotIn(private_key, serialized)
        self.assertEqual(self.store.application(self.application_id)["state"], "PREPARED")

    def test_same_review_cannot_renew_or_change_declaration(self):
        from services.applications.manual_auth import ManualAuthCompletionError

        self.record()
        with self.assertRaises(ManualAuthCompletionError) as raised:
            self.record(expires_in_seconds=300)
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_CONFLICT")

    def test_route_mismatch_and_foreign_review_are_rejected(self):
        from services.applications.manual_auth import ManualAuthCompletionError

        with self.assertRaises(ManualAuthCompletionError) as route_error:
            self.record(expected_route_url="https://foreign.example/job")
        self.assertEqual(route_error.exception.code, "AUTH_COMPLETION_STALE")
        with self.assertRaises(ManualAuthCompletionError) as review_error:
            self.record(review_operation_id="foreign-review")
        self.assertEqual(review_error.exception.code, "AUTH_COMPLETION_NOT_FOUND")

    def test_profile_route_or_cancelled_review_changes_invalidate_readback(self):
        from services.applications.manual_auth import (
            ManualAuthCompletionError, current_user_auth_completion,
        )

        self.record()
        profile = self.store.profile("fixture")
        profile["name"] = "Changed synthetic profile"
        self.store.save_profile(profile, expected_version=profile["version"])
        with self.assertRaises(ManualAuthCompletionError) as raised:
            current_user_auth_completion(
                self.store, "fixture", self.application_id, self.review_id,
            )
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_STALE")

    def test_tenant_change_invalidates_readback(self):
        from services.applications.manual_auth import (
            ManualAuthCompletionError, current_user_auth_completion,
        )

        self.record()
        route_id = self.store.application(self.application_id)["route_id"]
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == route_id,
            ).values(tenant="other"))
        with self.assertRaises(ManualAuthCompletionError) as raised:
            current_user_auth_completion(
                self.store, "fixture", self.application_id, self.review_id,
            )
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_STALE")

    def test_cancelled_review_cannot_receive_a_declaration(self):
        from services.applications.manual_auth import ManualAuthCompletionError

        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == self.review_id,
            ).values(cancel_requested=True))
        with self.assertRaises(ManualAuthCompletionError) as raised:
            self.record()
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_REVIEW_CANCELLED")

    def test_declaration_does_not_clear_auth_required_application_state(self):
        from services.applications.manual_auth import current_user_auth_completion

        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(state="AUTH_REQUIRED"))
        declared = self.record()
        self.assertFalse(declared["authenticated_session_available"])
        self.assertEqual(self.store.application(self.application_id)["state"], "AUTH_REQUIRED")
        self.assertEqual(current_user_auth_completion(
            self.store, "fixture", self.application_id, self.review_id,
        ), declared)

    def test_revocation_is_durable_and_cannot_be_undone(self):
        from services.applications.manual_auth import (
            ManualAuthCompletionError, current_user_auth_completion,
            revoke_user_auth_completion,
        )

        self.record()
        revoked = revoke_user_auth_completion(
            self.store, "fixture", self.application_id, self.review_id,
        )
        self.assertEqual(revoked["status"], "REVOKED")
        self.assertFalse(revoked["authenticated_session_available"])
        with self.assertRaises(ManualAuthCompletionError) as raised:
            current_user_auth_completion(
                self.store, "fixture", self.application_id, self.review_id,
            )
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_REVOKED")
        with self.assertRaises(ManualAuthCompletionError) as duplicate:
            self.record()
        self.assertEqual(duplicate.exception.code, "AUTH_COMPLETION_REVOKED")

    def test_expiry_fails_closed_without_extending_the_saved_declaration(self):
        from services.applications.manual_auth import (
            ManualAuthCompletionError, current_user_auth_completion,
        )

        self.record(expires_in_seconds=1)
        from datetime import datetime, timedelta, timezone

        expired_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        with patch("services.applications.manual_auth._utc_now", return_value=expired_at):
            with self.assertRaises(ManualAuthCompletionError) as raised:
                current_user_auth_completion(
                    self.store, "fixture", self.application_id, self.review_id,
                )
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_EXPIRED")
        with self.assertRaises(ManualAuthCompletionError):
            self.record(expires_in_seconds=900)


if __name__ == "__main__":
    unittest.main()
