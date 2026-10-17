"""Durable auth declaration restoration fails closed on stale or expired state."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from sqlalchemy import update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import manual_auth
from tests.integration import test_application_form_review_flow as fixtures


@unittest.skipUnless(
    hasattr(manual_auth, "record_user_auth_completion"),
    "requires the T01 auth declaration contract to be integrated",
)
class ManualAuthCompletionRestoreIntegrationTests(unittest.TestCase):
    tearDown = fixtures.ApplicationFormReviewFlowTests.tearDown
    _new_store = fixtures.ApplicationFormReviewFlowTests._new_store

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
            json={"request_id": "fleet-t01-b-restore"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.review_id = response.json()["operation_id"]
        self.declaration = manual_auth.record_user_auth_completion(
            self.store, "fixture", self.application_id, self.review_id,
            expected_route_url=self.route_url, expires_in_seconds=60,
        )

    def restore(self, store=None):
        from services.applications.manual_auth_recovery import restore_user_auth_completion

        return restore_user_auth_completion(
            store or self.store, "fixture", self.application_id, self.review_id,
        )

    def test_declaration_restores_from_a_recreated_store_without_authenticating(self):
        self.store.engine.dispose()
        recreated = Store(self.database_url)
        try:
            restored = self.restore(recreated)

            self.assertEqual(restored["status"], "USER_DECLARED")
            self.assertEqual(restored["review_operation_id"], self.review_id)
            self.assertIs(restored["authenticated_session_available"], False)
            self.assertEqual(recreated.application(self.application_id)["state"], "PREPARED")
        finally:
            recreated.engine.dispose()

    def test_expired_declaration_is_not_restored_or_extended(self):
        from services.applications.manual_auth import ManualAuthCompletionError

        expired_at = datetime.now(timezone.utc) + timedelta(seconds=61)
        with patch("services.applications.manual_auth._utc_now", return_value=expired_at):
            with self.assertRaises(ManualAuthCompletionError) as raised:
                self.restore()
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_EXPIRED")

    def test_route_or_profile_change_makes_saved_declaration_stale(self):
        from services.applications.manual_auth import ManualAuthCompletionError

        profile = self.store.profile("fixture")
        profile["name"] = "Changed synthetic profile"
        self.store.save_profile(profile, expected_version=profile["version"])

        with self.assertRaises(ManualAuthCompletionError) as raised:
            self.restore()
        self.assertEqual(raised.exception.code, "AUTH_COMPLETION_STALE")
        self.assertEqual(self.store.application(self.application_id)["state"], "PREPARED")


if __name__ == "__main__":
    unittest.main()
