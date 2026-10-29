"""MJ-REQ-230: durable per-job live submission intent is consumed once."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import delete, func, select, update

from fixtures.mock_server import fixture_server
from packages.domain.safety import Policy
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.submission_admission import (
    authorize_submission,
    grant_live_submission_intent,
)
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


class LiveSubmissionIntentAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-live-intent-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        self.application_id = prepare(
            self.store, self.store.jobs("fixture")[0]["id"], "fixture", self.root)
        self.store.transition(
            self.application_id, ["PREPARED"], "READY_TO_SUBMIT")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id).values(target="REAL"))
            application = dict(connection.execute(select(m.applications).where(
                m.applications.c.id == self.application_id)).mappings().one())
            self.store._audit(
                connection, self.application_id, "APPLICATION_QUESTION_PREFLIGHT", {
                    "result": "PASS",
                    "reason": None,
                    "observed_at": "2026-10-29T05:59:00+00:00",
                    "profile_version": application["profile_version"],
                    "document_version": application["document_version"],
                    "route_id": application["route_id"],
                    "missing": [],
                    "answer_review": [],
                })
        self.now = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
        self.receipt_id = "live-intent-receipt-a"
        self.policy = Policy(external_submissions_enabled=True)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def grant(self, *, receipt_id=None, application_id=None, granted_at=None,
              expires_at=None, source_allowed=True):
        application_id = application_id or self.application_id
        application = self.store.application(application_id)
        return grant_live_submission_intent(
            self.store,
            receipt_id=receipt_id or self.receipt_id,
            profile_id=application["profile_id"],
            application_id=application_id,
            job_id=application["job_id"],
            granted_at=granted_at or self.now - timedelta(minutes=5),
            expires_at=expires_at or self.now + timedelta(minutes=5),
            source_allowed=source_allowed,
        )

    def authorize(self, *, application_id=None, receipt_id=None, policy=None,
                  barrier=None):
        with patch(
                "services.applications.submission_admission._utc_now",
                return_value=self.now):
            return authorize_submission(
                self.store,
                application_id or self.application_id,
                confidence=100,
                policy=self.policy if policy is None else policy,
                intent_receipt_id=receipt_id or self.receipt_id,
                barrier=barrier,
            )

    def consumption_count(self):
        with self.store.engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(m.audit).where(
                m.audit.c.action == "LIVE_SUBMISSION_INTENT_CONSUMED")).scalar_one()

    def reset_ready(self, application_id=None):
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == (application_id or self.application_id)
            ).values(state="READY_TO_SUBMIT", submitted_at=None))

    def second_real_application(self):
        jobs = self.store.jobs("fixture")
        other_job = next(job for job in jobs
                         if job["id"] != self.store.application(self.application_id)["job_id"])
        application_id = prepare(self.store, other_job["id"], "fixture", self.root)
        self.store.transition(application_id, ["PREPARED"], "READY_TO_SUBMIT")
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == application_id).values(target="REAL"))
        return application_id

    def test_sequential_replay_cannot_reuse_consumed_receipt(self):
        self.grant()

        first = self.authorize()
        self.reset_ready()
        replay = self.authorize()

        self.assertTrue(first.allowed)
        self.assertFalse(replay.allowed)
        self.assertIn("JOB_INTENT_ALREADY_CONSUMED", replay.reasons)
        self.assertEqual(self.consumption_count(), 1)

    def test_concurrent_replay_has_exactly_one_admission_and_consumption(self):
        self.grant()

        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = list(pool.map(lambda _: self.authorize(), range(2)))

        self.assertEqual(sum(decision.allowed for decision in decisions), 1)
        self.assertEqual(self.consumption_count(), 1)
        self.assertEqual(self.store.application(self.application_id)["state"], "SUBMITTING")

    def test_missing_cross_job_expired_and_malformed_receipts_fail_closed(self):
        missing = self.authorize(receipt_id="missing-receipt")
        self.assertFalse(missing.allowed)
        self.assertIn("DURABLE_JOB_INTENT_REQUIRED", missing.reasons)

        self.grant()
        other = self.second_real_application()
        cross_job = self.authorize(application_id=other)
        self.assertFalse(cross_job.allowed)
        self.assertIn("JOB_INTENT_MISMATCH", cross_job.reasons)

        expired_id = "expired-live-intent"
        self.grant(receipt_id=expired_id, expires_at=self.now)
        expired = self.authorize(receipt_id=expired_id)
        self.assertFalse(expired.allowed)
        self.assertIn("STALE_JOB_INTENT", expired.reasons)

        malformed_id = "malformed-live-intent"
        self.grant(receipt_id=malformed_id)
        with self.store.engine.begin() as connection:
            row = connection.execute(select(m.audit).where(
                m.audit.c.action == "LIVE_SUBMISSION_INTENT_GRANTED",
                m.audit.c.payload["receipt_id"].as_string() == malformed_id,
            )).mappings().one()
            connection.execute(update(m.audit).where(m.audit.c.id == row["id"]).values(
                payload={"receipt_id": malformed_id, "profile_id": "fixture"}))
        malformed = self.authorize(receipt_id=malformed_id)
        self.assertFalse(malformed.allowed)
        self.assertIn("INVALID_JOB_INTENT", malformed.reasons)
        self.assertEqual(self.consumption_count(), 0)

    def test_default_off_and_intervention_barriers_do_not_consume_intent(self):
        self.grant()

        disabled = self.authorize(policy=Policy())
        self.assertFalse(disabled.allowed)
        self.assertIn("EXTERNAL_SUBMISSION_DISABLED", disabled.reasons)

        for barrier in ("CAPTCHA", "MFA"):
            with self.subTest(barrier=barrier):
                blocked = self.authorize(barrier=barrier)
                self.assertFalse(blocked.allowed)
                self.assertIn("USER_INTERVENTION_REQUIRED", blocked.reasons)
        self.assertEqual(self.consumption_count(), 0)

    def test_invalid_persisted_target_fails_closed_without_consuming_intent(self):
        self.grant()
        with self.store.engine.begin() as connection:
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id
            ).values(target="CORRUPTED"))

        decision = self.authorize()

        self.assertFalse(decision.allowed)
        self.assertIn("INVALID_SUBMISSION_TARGET", decision.reasons)
        self.assertEqual(
            self.store.application(self.application_id)["state"], "READY_TO_SUBMIT")
        self.assertEqual(self.consumption_count(), 0)

    def test_caller_cannot_override_the_admission_clock(self):
        self.grant()

        with self.assertRaises(TypeError):
            authorize_submission(
                self.store,
                self.application_id,
                confidence=100,
                policy=self.policy,
                intent_receipt_id=self.receipt_id,
                evaluated_at=self.now,
            )

        self.assertEqual(
            self.store.application(self.application_id)["state"], "READY_TO_SUBMIT")
        self.assertEqual(self.consumption_count(), 0)

    def test_missing_current_validated_materials_fail_all_material_gates(self):
        self.grant()
        application = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(delete(m.audit).where(
                m.audit.c.entity_id == self.application_id,
                m.audit.c.action.in_([
                    "DOCUMENTS_VALIDATED", "APPLICATION_QUESTION_PREFLIGHT",
                ]),
            ))
            connection.execute(update(m.facts).where(
                m.facts.c.profile_id == application["profile_id"],
                m.facts.c.version == application["profile_version"],
                m.facts.c.fact_id.in_(application["fact_ids"]),
            ).values(verified=False))

        decision = self.authorize()

        self.assertFalse(decision.allowed)
        self.assertIn("QUALITY_REVIEW_REQUIRED", decision.reasons)
        self.assertIn("UNVERIFIED_FACTS", decision.reasons)
        self.assertIn("SENSITIVE_REVIEW_REQUIRED", decision.reasons)
        self.assertEqual(
            self.store.application(self.application_id)["state"], "READY_TO_SUBMIT")
        self.assertEqual(self.consumption_count(), 0)


if __name__ == "__main__":
    unittest.main()
