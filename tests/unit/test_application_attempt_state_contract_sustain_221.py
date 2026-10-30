"""Synthetic SQLite acceptance for MJ-REQ-220 application states."""
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import Store, upsert
from services import audit_history  # noqa: F401 - register the durable audit table before migration
from services.applications.attempt_history import (
    AttemptConflict,
    finish_attempt,
    read_attempt_history,
    start_attempt,
)


class ApplicationAttemptStateContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(f"sqlite:///{(Path(self.temp.name) / 'state.db').as_posix()}")
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        with self.store.engine.begin() as connection:
            upsert(connection, m.profiles, {
                "id": "profile", "name": "Synthetic User", "email": "user@example.test",
                "version": "profile-v1",
            })
            upsert(connection, m.companies, {
                "id": "company", "name": "Synthetic Employer", "domain": "example.test",
            })
            upsert(connection, m.jobs, {
                "id": "job", "company_id": "company", "requisition": "REQ-1",
                "title": "Synthetic Role", "description": "Synthetic fixture",
                "skills": [], "location": "Remote", "remote_type": "REMOTE",
                "attributes": {},
            })
            upsert(connection, m.routes, {
                "id": "route", "job_id": "job", "company_id": "company",
                "provider": "mock-ats", "tenant": "synthetic", "requisition": "REQ-1",
                "url": "https://mock.invalid/apply/REQ-1",
            })
            upsert(connection, m.applications, {
                "id": "application", "job_id": "job", "route_id": "route",
                "profile_id": "profile", "profile_version": "profile-v1",
                "state": "PREPARED", "target": "MOCK", "fact_ids": [], "trace": [],
                "created_at": "2026-10-25T00:00:00Z", "submitted_at": None,
                "confirmation": None,
            })

    def application(self):
        return self.store.application("application")

    def route(self):
        with self.store.engine.connect() as connection:
            row = connection.execute(select(m.routes).where(
                m.routes.c.id == "route")).mappings().one()
        return dict(row)

    def start(self, request_id):
        return start_attempt(
            self.store, self.application(), self.route(), [],
            request_id=request_id, submit=False,
        )

    def test_valid_compare_and_swap_transition_persists_state_and_trace(self):
        self.store.transition("application", ["PREPARED"], "READY_TO_SUBMIT", "VALIDATE")

        saved = self.application()
        self.assertEqual(saved["state"], "READY_TO_SUBMIT")
        self.assertEqual(saved["trace"], ["VALIDATE"])

    def test_invalid_expected_state_transition_leaves_state_and_audit_unchanged(self):
        audit_count = self.store.count("audit_events")

        with self.assertRaisesRegex(ValueError, "Unsafe application transition"):
            self.store.transition("application", ["READY_TO_SUBMIT"], "SUBMITTING", "SUBMIT")

        self.assertEqual(self.application()["state"], "PREPARED")
        self.assertEqual(self.store.count("audit_events"), audit_count)

    def test_failed_attempt_requires_new_request_id_for_a_retry(self):
        first = self.start("attempt-first")
        finish_attempt(self.store, first["id"], self.application(), error=RuntimeError("synthetic"))

        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_ATTEMPT_REVIEW_REQUIRED"):
            self.start("attempt-first")

        retried = self.start("attempt-retry")
        self.assertFalse(retried["replayed"])
        self.assertNotEqual(retried["id"], first["id"])
        self.assertEqual(self.store.count("application_attempts"), 2)

    def test_captcha_pause_is_durable_user_intervention_not_confirmation(self):
        attempt = self.start("captcha-pause")
        self.store.transition("application", ["PREPARED"], "USER_INTERVENTION_REQUIRED", "DETECT_FORM")
        finish_attempt(self.store, attempt["id"], self.application())

        history = read_attempt_history(self.store, "profile", "application")["attempts"]
        self.assertEqual(self.application()["state"], "USER_INTERVENTION_REQUIRED")
        self.assertEqual(history[0]["state"], "USER_INTERVENTION_REQUIRED")
        self.assertEqual(history[0]["error_code"], "USER_INTERVENTION_REQUIRED")
        self.assertIsNone(history[0]["confirmation"])

    def test_login_pause_is_durable_auth_required_not_confirmation(self):
        attempt = self.start("login-pause")
        self.store.transition("application", ["PREPARED"], "AUTH_REQUIRED", "NAVIGATE")
        finish_attempt(self.store, attempt["id"], self.application())

        history = read_attempt_history(self.store, "profile", "application")["attempts"]
        self.assertEqual(self.application()["state"], "AUTH_REQUIRED")
        self.assertEqual(history[0]["state"], "AUTH_REQUIRED")
        self.assertEqual(history[0]["error_code"], "AUTH_REQUIRED")
        self.assertIsNone(history[0]["confirmation"])

    def test_submission_uncertainty_blocks_blind_retry(self):
        self.store.transition("application", ["PREPARED"], "SUBMITTING", "SUBMIT")
        self.store.transition("application", ["SUBMITTING"], "SUBMISSION_UNCONFIRMED")

        with self.assertRaisesRegex(ValueError, "reconcile before retry"):
            self.store.transition(
                "application", ["SUBMISSION_UNCONFIRMED"], "READY_TO_SUBMIT", "SUBMIT")
        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_ATTEMPT_STATE_CHANGED"):
            self.start("blind-retry")
        self.assertEqual(self.application()["state"], "SUBMISSION_UNCONFIRMED")

    def test_only_receipt_bearing_reconciliation_can_confirm_uncertain_submission(self):
        self.store.transition("application", ["PREPARED"], "SUBMITTING", "SUBMIT")
        self.store.transition("application", ["SUBMITTING"], "SUBMISSION_UNCONFIRMED")

        with self.assertRaisesRegex(ValueError, "receipt-bearing confirmation"):
            self.store.transition("application", ["SUBMISSION_UNCONFIRMED"], "CONFIRMED")
        self.assertEqual(self.application()["state"], "SUBMISSION_UNCONFIRMED")

        receipt = {"receipt": "MOCK-RECEIPT-1"}
        self.store.transition(
            "application", ["SUBMISSION_UNCONFIRMED"], "CONFIRMED",
            "VERIFY_CONFIRMATION", receipt,
        )
        saved = self.application()
        self.assertEqual(saved["state"], "CONFIRMED")
        self.assertEqual(saved["confirmation"], receipt)


if __name__ == "__main__":
    unittest.main()
