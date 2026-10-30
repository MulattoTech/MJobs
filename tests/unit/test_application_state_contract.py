"""Unit contract for durable, fail-closed application state changes."""
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select, update

from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.attempt_history import AttemptConflict
from services.applications.run_requests import queue_mock_run, validate_run_operation
from tests.integration.test_spine import PROFILE


class ApplicationStateContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-application-state-")
        database = Path(self.directory.name) / "state.db"
        self.store = Store(f"sqlite:///{database.as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        self.application_id = "application-state-fixture"
        profile_version = self.store.profile("fixture")["version"]
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id="state-company", name="State Fixture", domain="state.example.test",
            ))
            connection.execute(m.jobs.insert().values(
                id="state-job", company_id="state-company", requisition="STATE-1",
                title="State Contract Role", description="Synthetic state fixture",
                skills=[], location="Remote", remote_type="REMOTE", attributes={},
            ))
            connection.execute(m.routes.insert().values(
                id="state-route", job_id="state-job", company_id="state-company",
                provider="mock-ats", tenant="state-fixture", requisition="STATE-1",
                url="http://127.0.0.1:1/mock/state",
            ))
            connection.execute(m.applications.insert().values(
                id=self.application_id,
                job_id="state-job",
                route_id="state-route",
                profile_id="fixture",
                profile_version=profile_version,
                state="PREPARED",
                target="MOCK",
                fact_ids=[],
                trace=[],
                document_version=1,
                confirmation=None,
                created_at="2026-10-24T00:00:00Z",
                submitted_at=None,
            ))
            for kind in ("resume", "cover-letter"):
                connection.execute(m.documents.insert().values(
                    id=f"state-document-{kind}",
                    application_id=self.application_id,
                    kind=kind,
                    version=1,
                    profile_version=profile_version,
                    files={"pdf": f"synthetic-{kind}.pdf"},
                    fact_ids=[],
                    sha256={"pdf": "0" * 64},
                    template="synthetic",
                    created_at="2026-10-24T00:00:00Z",
                    generation_model=None,
                    generation_context={},
                ))

    def tearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    def application(self):
        return self.store.application(self.application_id)

    def application_audit_count(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(m.audit).where(
                m.audit.c.entity_id == self.application_id,
            )).fetchall().__len__()

    def operation(self, identifier):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == identifier,
            )).mappings().one())

    def assert_rejected_without_durable_change(self, action, error):
        before_application = self.application()
        before_audits = self.application_audit_count()
        before_operations = self.store.count("worker_operations")

        with self.assertRaisesRegex(error[0], error[1]):
            action()

        self.assertEqual(self.application(), before_application)
        self.assertEqual(self.application_audit_count(), before_audits)
        self.assertEqual(self.store.count("worker_operations"), before_operations)

    def test_reviewed_dry_run_path_persists_each_forward_transition(self):
        expected = (
            ("PREPARED", "FILLING"),
            ("FILLING", "READY_TO_SUBMIT"),
            ("READY_TO_SUBMIT", "DRY_RUN_COMPLETE"),
        )

        for previous, state in expected:
            with self.subTest(state=state):
                before_audits = self.application_audit_count()
                self.store.transition(
                    self.application_id, [previous], state, step=f"STEP_{state}",
                )
                application = self.application()
                self.assertEqual(application["state"], state)
                self.assertEqual(application["trace"][-1], f"STEP_{state}")
                self.assertEqual(self.application_audit_count(), before_audits + 1)

        self.assertIsNone(self.application()["confirmation"])
        self.assertIsNone(self.application()["submitted_at"])
        self.assertEqual(self.store.count("worker_operations"), 0)

    def test_receipt_gates_confirmation_and_confirmed_state_is_terminal(self):
        self.store.transition(self.application_id, ["PREPARED"], "SUBMITTING")
        receipt = {
            "receipt": "MOCK-state-receipt",
            "reference_number": "MOCK-state-receipt",
            "timestamp": "2026-10-24T00:01:00Z",
        }
        self.store.transition(
            self.application_id,
            ["SUBMITTING"],
            "CONFIRMED",
            confirmation=receipt,
        )
        self.assertEqual(self.application()["state"], "CONFIRMED")
        self.assertEqual(self.application()["confirmation"], receipt)

        self.assert_rejected_without_durable_change(
            lambda: self.store.transition(
                self.application_id, ["CONFIRMED"], "PREPARED", step="MUTATE_TERMINAL",
            ),
            (ValueError, "Unsafe application transition"),
        )

    def test_state_replay_and_run_request_replay_are_idempotent(self):
        self.store.transition(self.application_id, ["PREPARED"], "FILLING", step="FILL")
        after_first = self.application()
        after_first_audits = self.application_audit_count()

        self.store.transition(self.application_id, ["FILLING"], "FILLING", step="FILL")

        self.assertEqual(self.application(), after_first)
        self.assertEqual(self.application_audit_count(), after_first_audits)

        self.store.transition(self.application_id, ["FILLING"], "PREPARED")
        first = queue_mock_run(
            self.store, "fixture", self.application_id,
            submit=False, request_id="same-reviewed-intent",
        )
        before_replay = self.application()
        before_replay_audits = self.application_audit_count()
        second = queue_mock_run(
            self.store, "fixture", self.application_id,
            submit=False, request_id="same-reviewed-intent",
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["operation_id"], first["operation_id"])
        self.assertEqual(self.application(), before_replay)
        self.assertEqual(self.application_audit_count(), before_replay_audits)
        self.assertEqual(self.store.count("worker_operations"), 1)

    def test_captcha_and_auth_pauses_create_no_submission_or_receipt(self):
        for pause in ("CAPTCHA_REQUIRED", "AUTH_REQUIRED"):
            with self.subTest(pause=pause):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.applications).where(
                        m.applications.c.id == self.application_id,
                    ).values(state="PREPARED", trace=[], confirmation=None, submitted_at=None))
                    connection.execute(m.audit.delete().where(
                        m.audit.c.entity_id == self.application_id,
                    ))
                    connection.execute(m.operations.delete())

                self.store.transition(self.application_id, ["PREPARED"], pause, step=pause)

                application = self.application()
                self.assertEqual(application["state"], pause)
                self.assertIsNone(application["confirmation"])
                self.assertIsNone(application["submitted_at"])
                self.assertEqual(self.store.count("worker_operations"), 0)

    def test_stale_reviewed_input_fails_before_attempt_or_state_change(self):
        queued = queue_mock_run(
            self.store, "fixture", self.application_id,
            submit=False, request_id="stale-reviewed-input",
        )
        before_state = self.application()["state"]
        before_audits = self.application_audit_count()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(document_version=2))

        with self.assertRaisesRegex(AttemptConflict, "APPLICATION_RUN_INPUT_CHANGED"):
            validate_run_operation(self.store, self.operation(queued["operation_id"]))

        self.assertEqual(self.application()["state"], before_state)
        self.assertEqual(self.application_audit_count(), before_audits)
        self.assertEqual(self.store.count("application_attempts"), 0)

    def test_invalid_back_transition_is_rejected_without_state_or_audit_change(self):
        self.store.transition(self.application_id, ["PREPARED"], "FILLING")
        self.store.transition(self.application_id, ["FILLING"], "READY_TO_SUBMIT")

        self.assert_rejected_without_durable_change(
            lambda: self.store.transition(
                self.application_id, ["READY_TO_SUBMIT"], "FILLING", step="GO_BACK",
            ),
            (ValueError, "Unsafe application transition"),
        )

    def test_stale_expected_state_is_rejected_without_state_or_audit_change(self):
        self.store.transition(self.application_id, ["PREPARED"], "FILLING")

        self.assert_rejected_without_durable_change(
            lambda: self.store.transition(
                self.application_id, ["PREPARED"], "READY_TO_SUBMIT", step="STALE",
            ),
            (ValueError, "Unsafe application transition from FILLING"),
        )

    def test_submission_retries_and_receiptless_confirmation_fail_closed(self):
        for state in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
            with self.subTest(state=state):
                with self.store.engine.begin() as connection:
                    connection.execute(update(m.applications).where(
                        m.applications.c.id == self.application_id,
                    ).values(state=state, confirmation=None))
                    connection.execute(m.audit.delete().where(
                        m.audit.c.entity_id == self.application_id,
                    ))
                    connection.execute(m.operations.delete())

                self.assert_rejected_without_durable_change(
                    lambda: queue_mock_run(
                        self.store, "fixture", self.application_id,
                        submit=True, request_id=f"retry-{state.lower()}",
                    ),
                    (AttemptConflict, "RECONCILIATION_REQUIRED"),
                )

        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(state="SUBMISSION_UNCONFIRMED", confirmation=None))
        for confirmation in (None, {}, {"reference_number": "missing-receipt"}):
            with self.subTest(confirmation=confirmation):
                self.assert_rejected_without_durable_change(
                    lambda value=confirmation: self.store.transition(
                        self.application_id,
                        ["SUBMISSION_UNCONFIRMED"],
                        "CONFIRMED",
                        confirmation=value,
                    ),
                    (ValueError, "receipt-bearing confirmation"),
                )


if __name__ == "__main__":
    unittest.main()
