"""MJ-REQ-151: rolling company capacity preserves uncertain reservations.

Exercise durable admission only. No test performs a browser submission or an
employer request. Seed fictional, already prepared MOCK inputs into the real
schema so admission runs its production SQL, locking and audit implementation
without depending on document generation or the API application.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, update

from packages.domain.safety import Policy
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications import submission_admission as admission_module
from services.applications.submission_admission import authorize_submission


EVALUATED_AT = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
CUTOFF = EVALUATED_AT - timedelta(days=1)


class CompanySubmissionWindowTests(unittest.TestCase):
    def _new_store(self):
        store = Store(f"sqlite:///{(self.root / 'company-window.db').as_posix()}")
        store.migrate()
        return store

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-company-window-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self._new_store()
        self.addCleanup(self.store.engine.dispose)
        self.store.bind_workspace_mode("FIXTURE")
        self.profile_id = "company-window-profile"
        self.company_id = "company-window-company"
        self._seed_profile(self.profile_id)
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=self.company_id, name="Fictional Window Company",
                domain="company-window.example.test",
            ))
        self.first = self._seed_application("first")
        self.second = self._seed_application("second")
        self.applications = [self.first["id"], self.second["id"]]
        self.policy = Policy(hourly_limit=10000, daily_limit=10000, company_limit=1)

    def _seed_profile(self, identifier):
        with self.store.engine.begin() as connection:
            connection.execute(m.profiles.insert().values(
                id=identifier, name="Fictional Window Applicant",
                email=f"{identifier}@example.test", version="v1",
            ))
            connection.execute(m.profile_versions.insert().values(
                id=f"{identifier}-v1", profile_id=identifier, version="v1",
                contact={}, answers=[],
            ))

    def _seed_application(self, suffix, *, state="READY_TO_SUBMIT", submitted_at=None):
        job_id = f"company-window-job-{suffix}"
        route_id = f"company-window-route-{suffix}"
        application_id = f"company-window-application-{suffix}"
        with self.store.engine.begin() as connection:
            connection.execute(m.jobs.insert().values(
                id=job_id, company_id=self.company_id, requisition=suffix,
                title=f"Fictional Test Role {suffix}", description="Synthetic admission fixture.",
                skills=[], location="Fixture", remote_type="REMOTE", attributes={"status": "OPEN"},
            ))
            connection.execute(m.routes.insert().values(
                id=route_id, job_id=job_id, company_id=self.company_id,
                provider="mock-ats", tenant="company-window", requisition=suffix,
                url=f"http://127.0.0.1:9/ats/apply/{suffix}",
            ))
            connection.execute(m.matches.insert().values(
                id=f"company-window-match-{suffix}", job_id=job_id,
                profile_id=self.profile_id, profile_version="v1",
                score=100, missing=[], components={},
            ))
            connection.execute(m.applications.insert().values(
                id=application_id, job_id=job_id, route_id=route_id,
                profile_id=self.profile_id, profile_version="v1", state=state,
                target="MOCK", fact_ids=[], trace=[], document_version=1,
                confirmation=None, created_at=(EVALUATED_AT - timedelta(days=10)).isoformat(),
                submitted_at=submitted_at,
            ))
        return self.store.application(application_id)

    def _prior(self, state, submitted_at):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.first["id"],
            ).values(state=state, submitted_at=submitted_at))

    def _admit(self, identifier=None, *, store=None, check=None):
        with patch("services.applications.submission_admission._utc_now",
                   return_value=EVALUATED_AT), \
                patch("services.applications.submission_admission.now",
                      return_value=EVALUATED_AT.isoformat()):
            return authorize_submission(
                store if store is not None else self.store,
                identifier if identifier is not None else self.second["id"],
                100, policy=self.policy, check=check,
            )

    def _assert_company_blocked(self):
        decision = self._admit()
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reasons, ("COMPANY_LIMIT_REACHED",))
        current = self.store.application(self.second["id"])
        self.assertEqual(current["state"], "READY_TO_SUBMIT")
        self.assertIsNone(current["submitted_at"])

    def _submission_audits(self, store):
        with store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action == "SUBMITTING",
                m.audit.c.entity_id.in_(self.applications),
            )).scalar_one()

    def test_confirmed_submission_expires_strictly_before_inclusive_cutoff(self):
        for submitted_at in (
                CUTOFF.isoformat(),
                (CUTOFF + timedelta(microseconds=1)).isoformat(),
                CUTOFF.isoformat().replace("+00:00", "Z")):
            with self.subTest(submitted_at=submitted_at):
                self._prior("CONFIRMED", submitted_at)
                self._assert_company_blocked()
        self._prior("CONFIRMED", (CUTOFF - timedelta(microseconds=1)).isoformat())
        decision = self._admit()
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertEqual(self.store.application(self.second["id"])["state"], "SUBMITTING")
        self.assertEqual(self._submission_audits(self.store), 1)

    def test_aware_offsets_are_compared_as_instants_not_strings(self):
        # The within-window string sorts BEFORE the UTC cutoff; the expired
        # string sorts AFTER it. Lexical comparisons get both decisions wrong.
        within_window = (CUTOFF + timedelta(minutes=1)).astimezone(
            timezone(timedelta(hours=-6))).isoformat()
        expired = (CUTOFF - timedelta(minutes=1)).astimezone(
            timezone(timedelta(hours=5, minutes=30))).isoformat()
        self.assertLess(within_window, CUTOFF.isoformat())
        self.assertGreater(expired, CUTOFF.isoformat())
        self._prior("CONFIRMED", within_window)
        self._assert_company_blocked()
        self._prior("CONFIRMED", expired)
        decision = self._admit()
        self.assertTrue(decision.allowed, decision.reasons)

    def test_unresolved_reservations_do_not_expire_or_require_a_date(self):
        for state in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
            for submitted_at in (
                    (EVALUATED_AT - timedelta(days=7)).isoformat(), None, ""):
                with self.subTest(state=state, submitted_at=submitted_at):
                    self._prior(state, submitted_at)
                    self._assert_company_blocked()
        self.assertEqual(self._submission_audits(self.store), 0)

    def test_confirmed_unknown_naive_or_future_dates_keep_reservations(self):
        for submitted_at in (
                None, "", "not-a-date", "2026-99-01T00:00:00+00:00",
                (CUTOFF - timedelta(days=7)).replace(tzinfo=None).isoformat(),
                (EVALUATED_AT + timedelta(days=7)).isoformat()):
            with self.subTest(submitted_at=submitted_at):
                self._prior("CONFIRMED", submitted_at)
                self._assert_company_blocked()
        self.assertEqual(self._submission_audits(self.store), 0)

    def test_other_profiles_unresolved_reservation_does_not_consume_capacity(self):
        other_profile_id = "company-window-other-profile"
        self._seed_profile(other_profile_id)
        self._prior("SUBMISSION_UNCONFIRMED", None)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.first["id"],
            ).values(profile_id=other_profile_id, profile_version="v1"))
        decision = self._admit()
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertEqual(self.store.application(self.first["id"])["state"],
                         "SUBMISSION_UNCONFIRMED")

    def test_other_company_reservation_does_not_consume_capacity(self):
        other_company_id = "company-window-other-company"
        self._prior("SUBMITTING", EVALUATED_AT.isoformat())
        with self.store.engine.begin() as connection:
            connection.execute(m.companies.insert().values(
                id=other_company_id, name="Other Fictional Company",
                domain="other-company.example.test",
            ))
            connection.execute(update(m.jobs).where(
                m.jobs.c.id == self.first["job_id"],
            ).values(company_id=other_company_id))
            connection.execute(update(m.routes).where(
                m.routes.c.job_id == self.first["job_id"],
            ).values(company_id=other_company_id))
        self.assertNotEqual(other_company_id, self.company_id)
        decision = self._admit()
        self.assertTrue(decision.allowed, decision.reasons)

    def test_concurrent_same_company_admissions_share_one_durable_slot(self):
        self._prior("CONFIRMED", (CUTOFF - timedelta(days=2)).isoformat())
        third = self._seed_application("third")
        self.applications.append(third["id"])
        candidates = [self.second["id"], third["id"]]
        start = Barrier(2)

        def admit(identifier):
            start.wait(timeout=10)
            return authorize_submission(self.store, identifier, 100, policy=self.policy)

        with patch("services.applications.submission_admission._utc_now",
                   return_value=EVALUATED_AT), \
                patch("services.applications.submission_admission.now",
                      return_value=EVALUATED_AT.isoformat()):
            with ThreadPoolExecutor(max_workers=2) as pool:
                decisions = list(pool.map(admit, candidates))
        self.assertEqual(sum(decision.allowed for decision in decisions), 1)
        denied = next(decision for decision in decisions if not decision.allowed)
        self.assertEqual(denied.reasons, ("COMPANY_LIMIT_REACHED",))
        reopened = Store(self.store.url)
        try:
            applications = [reopened.application(identifier) for identifier in candidates]
            self.assertCountEqual([row["state"] for row in applications],
                                  ["SUBMITTING", "READY_TO_SUBMIT"])
            reserved = next(row for row in applications if row["state"] == "SUBMITTING")
            waiting = next(row for row in applications if row["state"] == "READY_TO_SUBMIT")
            self.assertEqual(reserved["submitted_at"], EVALUATED_AT.isoformat())
            self.assertIsNone(waiting["submitted_at"])
            self.assertEqual(reopened.application(self.first["id"])["state"], "CONFIRMED")
            self.assertEqual(self._submission_audits(reopened), 1)
            repeated = self._admit(waiting["id"], store=reopened)
            self.assertFalse(repeated.allowed)
            self.assertEqual(repeated.reasons, ("COMPANY_LIMIT_REACHED",))
            self.assertEqual(self._submission_audits(reopened), 1)
        finally:
            reopened.engine.dispose()

    def test_cancellation_during_company_history_scan_rolls_back_and_releases_connection(self):
        old = (CUTOFF - timedelta(days=2)).isoformat()
        self._prior("CONFIRMED", old)
        historical = self._seed_application("history", state="CONFIRMED", submitted_at=old)
        self.applications.append(historical["id"])
        before = [self.store.application(identifier) for identifier in self.applications]
        inspected = []
        real_window_check = admission_module.company_submission_reserves_capacity

        class ScanCancelled(Exception):
            pass

        def observe_window_check(state, submitted_at, *, evaluated_at):
            inspected.append((state, submitted_at))
            return real_window_check(state, submitted_at, evaluated_at=evaluated_at)

        def cancel_after_first_history_row():
            if inspected:
                raise ScanCancelled("Company history scan cancelled")

        with patch.object(admission_module, "company_submission_reserves_capacity",
                          side_effect=observe_window_check):
            with self.assertRaisesRegex(ScanCancelled, "Company history scan cancelled"):
                self._admit(check=cancel_after_first_history_row)
        self.assertEqual(len(inspected), 1)
        self.assertEqual(
            [self.store.application(identifier) for identifier in self.applications], before,
        )
        self.assertEqual(self._submission_audits(self.store), 0)
        # A new transaction can read and then claim capacity after cancellation;
        # the streaming cursor/transaction must not leave the connection locked.
        decision = self._admit()
        self.assertTrue(decision.allowed, decision.reasons)
        self.assertEqual(self._submission_audits(self.store), 1)

    def test_same_application_cannot_be_readmitted_after_store_reopen(self):
        first_decision = self._admit(self.first["id"])
        self.assertTrue(first_decision.allowed, first_decision.reasons)
        reopened = Store(self.store.url)
        try:
            repeated = self._admit(self.first["id"], store=reopened)
            self.assertFalse(repeated.allowed)
            self.assertIn("INVALID_SUBMISSION_STATE", repeated.reasons)
            current = reopened.application(self.first["id"])
            self.assertEqual(current["state"], "SUBMITTING")
            self.assertEqual(current["trace"].count("SUBMIT"), 1)
            self.assertEqual(current["submitted_at"], EVALUATED_AT.isoformat())
            self.assertEqual(self._submission_audits(reopened), 1)
        finally:
            reopened.engine.dispose()

    def test_expired_confirmed_capacity_does_not_enable_external_submission(self):
        self._prior("CONFIRMED", (EVALUATED_AT - timedelta(days=2)).isoformat())
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.second["id"],
            ).values(target="REAL"))
        decision = self._admit()
        self.assertFalse(decision.allowed)
        self.assertIn("EXTERNAL_SUBMISSION_DISABLED", decision.reasons)
        self.assertNotIn("COMPANY_LIMIT_REACHED", decision.reasons)
        self.assertEqual(self.store.application(self.second["id"])["state"], "READY_TO_SUBMIT")
        self.assertEqual(self._submission_audits(self.store), 0)


@unittest.skipUnless(os.environ.get("MJOBS_TEST_POSTGRES_URL"),
                     "Set MJOBS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database")
class CompanySubmissionWindowPostgresTests(CompanySubmissionWindowTests):
    def _new_store(self):
        # Import the broader fixture only when the disposable PostgreSQL test is
        # actually enabled. Skipped tests do not load the API/document stack.
        from tests.integration.test_lever_form_review_flow import LeverFormReviewPostgresFlowTests
        return LeverFormReviewPostgresFlowTests._new_store(self)

    def _drop_schema(self):
        from tests.integration.test_lever_form_review_flow import LeverFormReviewPostgresFlowTests
        return LeverFormReviewPostgresFlowTests._drop_schema(self)


if __name__ == "__main__":
    unittest.main()
